from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from sova.qualification.http_orders import OrderHttpClient
from sova.qualification.__main__ import main
from sova.qualification.requests import OrderIntent, RequestJournal, reconcile


INTENT = OrderIntent("request-1", "customer-1", "key-1", "12.50")
RECEIPT = {"order_id": 100, "customer": "customer-1", "idempotency_key": "key-1",
           "amount": "12.50", "result": "confirmed"}
ORDER = dict(RECEIPT, work_items=1, done_items=1)


@pytest.fixture
def journal(tmp_path):
    j = RequestJournal.create(tmp_path / "requests.sqlite", run_id="run-1", tenant_id="tenant-1",
                              profile_digest="a" * 64)
    yield j
    j.close()


def observation(journal, rows=None, *, complete=True):
    return {"scope": dict(journal.scope), "complete": complete,
            "orders": deepcopy([ORDER] if rows is None else rows)}


def success(journal, *, kind="submit"):
    journal.request(INTENT)
    a = journal.begin(INTENT.request_id, kind=kind)
    journal.finish(a, status=201 if kind == "submit" else 200, receipt=RECEIPT)


def test_records_survive_reopening_and_do_not_overwrite_intent(journal):
    success(journal)
    path = journal.path
    reopened = RequestJournal(path)
    try:
        assert reopened.snapshot() == journal.snapshot()
        with pytest.raises(ValueError, match="another intent"):
            reopened.request(OrderIntent("request-1", "customer-1", "key-1", "50"))
        with pytest.raises(FileExistsError):
            RequestJournal.create(path, run_id="run-1", tenant_id="tenant-1", profile_digest="a" * 64)
    finally:
        reopened.close()


def test_journal_is_append_only_and_does_not_follow_symlinks(journal, tmp_path):
    success(journal)
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        journal.db.execute("DELETE FROM outcomes")
    journal.db.rollback()
    alias = tmp_path / "alias"
    alias.symlink_to(journal.path)
    with pytest.raises(ValueError, match="regular"):
        RequestJournal(alias)


@pytest.mark.parametrize("value", [True, 12.5, "NaN", "Infinity", "-1", "0", "1.001", "1e2", "01", "9" * 20])
def test_invalid_money_is_not_coerced(value):
    with pytest.raises(ValueError):
        OrderIntent("r", "c", "k", value)


def test_the_same_business_key_cannot_change_amount(journal):
    journal.request(INTENT)
    with pytest.raises(ValueError, match="another amount"):
        journal.request(OrderIntent("another-request", "customer-1", "key-1", "13"))


def test_normal_receipt_matches_independent_business_result(journal):
    success(journal)
    report = reconcile(journal.snapshot(), observation(journal))
    assert report["data_integrity"] == "SATISFIED"
    assert report["requests"][0]["resolution"] == "CONFIRMED"
    assert report["requests"][0]["total_completion_ms"] >= 0
    assert report["qualification_issued"] is False
    assert report["authorizes_execution"] is False
    assert report["service_continuity"] == "NOT_QUALIFIED"


@pytest.mark.parametrize("change,reason", [
    (lambda rows: rows.clear(), "ACKNOWLEDGED_RESULT_MISSING"),
    (lambda rows: rows.append(dict(rows[0])), "DUPLICATE_BUSINESS_RESULT"),
    (lambda rows: rows[0].update(amount="100"), "BUSINESS_RESULT_MISMATCH"),
    (lambda rows: rows[0].update(amount="12.501"), "BUSINESS_RESULT_MISMATCH"),
    (lambda rows: rows[0].update(work_items=2), "BUSINESS_EFFECT_MISMATCH"),
    (lambda rows: rows[0].update(done_items=0), "BUSINESS_EFFECT_MISMATCH"),
    (lambda rows: rows[0].update(result=None), "BUSINESS_RESULT_MISMATCH"),
    (lambda rows: rows[0].update(order_id=101), "RECEIPT_ORDER_MISMATCH"),
])
def test_missing_duplicate_or_changed_result_is_detected(journal, change, reason):
    success(journal)
    observed = observation(journal)
    change(observed["orders"])
    report = reconcile(journal.snapshot(), observed)
    assert report["data_integrity"] == "VIOLATION"
    assert reason in report["requests"][0]["violations"]


def test_commit_without_response_stays_unresolved_until_client_lookup(journal):
    journal.request(INTENT)
    journal.finish(journal.begin(INTENT.request_id), error="transport_error")
    report = reconcile(journal.snapshot(), observation(journal))
    assert report["requests"][0]["resolution"] == "UNRESOLVED_COMMIT"
    assert report["counts"]["transport_error_attempts"] == 1
    success(journal, kind="lookup")
    report = reconcile(journal.snapshot(), observation(journal))
    assert report["requests"][0]["resolution"] == "CONFIRMED"
    assert report["counts"]["successful_submission_responses"] == 0
    assert report["counts"]["transport_error_attempts"] == 1


def test_retry_preserves_first_error_and_full_completion_time(tmp_path):
    clock_value = [1_000_000]
    journal = RequestJournal.create(tmp_path / "j", run_id="r", tenant_id="t", profile_digest="a" * 64,
                                    clock=lambda: clock_value[0])
    try:
        journal.request(INTENT)
        first = journal.begin(INTENT.request_id)
        clock_value[0] += 5_000_000
        journal.finish(first, status=503)
        clock_value[0] += 100_000_000
        second = journal.begin(INTENT.request_id)
        clock_value[0] += 2_000_000
        journal.finish(second, status=201, receipt=RECEIPT)
        report = reconcile(journal.snapshot(), observation(journal))
        assert report["counts"]["submission_attempts"] == 2
        assert report["counts"]["http_error_attempts"] == 1
        assert report["requests"][0]["total_completion_ms"] == 107
    finally:
        journal.close()


def test_process_restart_does_not_invent_a_cross_clock_duration(journal):
    journal.request(INTENT)
    journal.begin(INTENT.request_id)  # crash before receiving or journalling the reply
    reopened = RequestJournal(journal.path)
    try:
        success(reopened, kind="lookup")
        report = reconcile(reopened.snapshot(), observation(reopened))
        assert report["requests"][0]["resolution"] == "CONFIRMED"
        assert report["requests"][0]["total_completion_ms"] is None
        assert report["counts"]["unfinished_attempts"] == 1
    finally:
        reopened.close()


def test_incomplete_observation_does_not_turn_missing_data_into_a_verdict(journal):
    success(journal)
    report = reconcile(journal.snapshot(), observation(journal, [], complete=False))
    assert report["data_integrity"] == "UNKNOWN"
    assert report["requests"][0]["resolution"] == "UNKNOWN"


def test_empty_run_does_not_pass(journal):
    report = reconcile(journal.snapshot(), observation(journal, []))
    assert report["data_integrity"] == "UNKNOWN"


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "profile_digest"])
def test_another_scope_is_rejected(journal, field):
    success(journal)
    observed = observation(journal)
    observed["scope"][field] = "other"
    with pytest.raises(ValueError, match="exact run"):
        reconcile(journal.snapshot(), observed)


def test_a_second_customer_cannot_be_used_as_a_receipt(journal):
    journal.request(INTENT)
    a = journal.begin(INTENT.request_id)
    with pytest.raises(ValueError, match="Receipt"):
        journal.finish(a, status=200, receipt=dict(RECEIPT, customer="other"))
    journal.finish(a, status=200, error="invalid_receipt")
    assert reconcile(journal.snapshot(), observation(journal))["data_integrity"] == "VIOLATION"


def test_duplicate_outcomes_are_not_last_writer_wins(journal):
    success(journal)
    snapshot = journal.snapshot()
    snapshot["outcomes"].append(dict(snapshot["outcomes"][0]))
    with pytest.raises(ValueError, match="Duplicate outcomes"):
        reconcile(snapshot, observation(journal))


def test_a_timeout_can_have_committed_and_never_leaks_its_error(journal):
    def transport(request):
        assert len(journal.snapshot()["attempts"]) == 1  # durably saved before sending
        raise httpx.ReadTimeout("password=secret-at-private-host", request=request)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        sender = OrderHttpClient("https://orders.example", bearer_token="secret-token", journal=journal,
                                 client=client)
        assert sender.submit(INTENT) is None
    serialized = json.dumps(journal.snapshot())
    assert "secret" not in serialized and "private-host" not in serialized
    assert reconcile(journal.snapshot(), observation(journal))["requests"][0]["resolution"] == "UNRESOLVED_COMMIT"


@pytest.mark.parametrize("origin", ["http://10.0.0.1", "https://u:p@example.com", "https://example.com?token=x",
                                   "https://example.com/path", "file:///tmp/order"])
def test_plaintext_remote_or_credential_origins_are_rejected(journal, origin):
    with pytest.raises(ValueError):
        OrderHttpClient(origin, bearer_token="token", journal=journal)


@pytest.mark.parametrize("body", [b"not json", b"x" * 20000, b'{}'])
def test_broken_success_receipts_are_recorded_as_violations(journal, body):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))) as client:
        sender = OrderHttpClient("https://orders.example", bearer_token="token", journal=journal, client=client)
        assert sender.submit(INTENT) == 200
    assert reconcile(journal.snapshot(), observation(journal))["data_integrity"] == "VIOLATION"


def test_redirects_are_never_followed(journal):
    calls = []
    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://other.example"})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        sender = OrderHttpClient("https://orders.example", bearer_token="token", journal=journal, client=client)
        assert sender.submit(INTENT) == 307
    assert calls == ["https://orders.example/orders"]


def test_lookup_alone_cannot_confirm_an_order_from_before_the_run(journal):
    journal.request(INTENT)
    with pytest.raises(ValueError, match="previously recorded"):
        journal.begin(INTENT.request_id, kind="lookup")
    report = reconcile(journal.snapshot(), observation(journal, []))
    assert report["data_integrity"] == "UNKNOWN"


def test_clock_regression_cannot_hide_waiting_between_retries(tmp_path):
    clock = [1000]
    j = RequestJournal.create(tmp_path / "j", run_id="r", tenant_id="t", profile_digest="a" * 64,
                              clock=lambda: clock[0])
    try:
        j.request(INTENT)
        a = j.begin(INTENT.request_id)
        clock[0] = 2000
        j.finish(a, status=503)
        clock[0] = 1500  # still after intent, but before the previous reply
        with pytest.raises(ValueError, match="regressed"):
            j.begin(INTENT.request_id)
    finally:
        j.close()


@pytest.mark.parametrize("field", ["work_items", "done_items"])
def test_boolean_counts_do_not_prove_one_business_effect(journal, field):
    success(journal)
    observed = observation(journal)
    observed["orders"][0][field] = True
    assert reconcile(journal.snapshot(), observed)["data_integrity"] == "VIOLATION"


@pytest.mark.parametrize("status,error", [(200, "transport_error"), (503, "invalid_receipt"), (None, None)])
def test_inconsistent_outcomes_are_not_persisted(journal, status, error):
    journal.request(INTENT)
    a = journal.begin(INTENT.request_id)
    with pytest.raises(ValueError):
        journal.finish(a, status=status, error=error)
    assert journal.snapshot()["outcomes"] == []


@pytest.mark.parametrize("rows,expected", [([ORDER], 0), ([], 2)])
def test_cli_reports_measurement_and_never_issues_qualification(journal, tmp_path, capsys, rows, expected):
    success(journal)
    path = tmp_path / "observed.json"
    path.write_text(json.dumps(observation(journal, rows)))
    assert main(["reconcile", "--journal", str(journal.path), "--observation", str(path)]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["service_continuity"] == "NOT_QUALIFIED"
    assert result["qualification_issued"] is False


def test_cli_rejects_duplicate_keys_without_disclosing_input(journal, tmp_path, capsys):
    path = tmp_path / "observed.json"
    path.write_text('{"private-value": 1, "private-value": 2}')
    assert main(["reconcile", "--journal", str(journal.path), "--observation", str(path)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "private-value" not in output.err
    assert str(path) not in output.err


def test_an_alias_request_id_does_not_inflate_completed_business(journal):
    journal.request(INTENT)
    with pytest.raises(ValueError, match="another logical request"):
        journal.request(OrderIntent("alias", "customer-1", "key-1", "12.50"))
    assert len(journal.snapshot()["requests"]) == 1


def test_the_same_key_in_another_customer_is_a_different_business(journal):
    journal.request(INTENT)
    journal.request(OrderIntent("second-customer", "customer-2", "key-1", "12.50"))
    assert len(journal.snapshot()["requests"]) == 2


def test_observation_version_is_not_coerced_from_boolean(journal):
    success(journal)
    observed = observation(journal)
    observed["scope"]["version"] = True
    with pytest.raises(ValueError, match="exact run"):
        reconcile(journal.snapshot(), observed)


def test_cli_unresolved_commit_is_nonzero_even_when_the_data_exists(journal, tmp_path, capsys):
    journal.request(INTENT)
    journal.finish(journal.begin(INTENT.request_id), error="transport_error")
    path = tmp_path / "observed.json"
    path.write_text(json.dumps(observation(journal)))
    assert main(["reconcile", "--journal", str(journal.path), "--observation", str(path)]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["data_integrity"] == "SATISFIED"
    assert report["requests"][0]["resolution"] == "UNRESOLVED_COMMIT"
