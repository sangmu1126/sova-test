import asyncio
import json
import time

import httpx
import pytest

from sova.qualification.bounded_orders import BoundedOrderClient, CompletionBudget
from sova.qualification.requests import OrderIntent, RequestJournal


@pytest.fixture
def journal(tmp_path):
    value = RequestJournal.create(tmp_path / "requests.sqlite", run_id="retry-test", tenant_id="tenant-a", profile_digest="a"*64)
    yield value
    value.close()


INTENT = OrderIntent("request-1", "customer-a", "key-a", "12.34")
RECEIPT = {"order_id": 1, "customer": "customer-a", "idempotency_key": "key-a", "amount": "12.34", "result": "confirmed"}


def execute(journal, handler, *, budget=CompletionBudget(), events=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client = BoundedOrderClient("https://orders.fixture", bearer_token="fixture-token", journal=journal,
                client=transport, budget=budget, jitter=lambda a,b: 0, observer=(events.append if events is not None else None))
            return await client.complete(INTENT)
    return asyncio.run(run())


def test_transient_post_is_looked_up_before_same_key_resubmission(journal):
    methods = []
    async def handler(request):
        methods.append(request.method)
        if len(methods) == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        if len(methods) == 2:
            return httpx.Response(404)
        assert json.loads(request.content)["amount"] == "12.34"
        assert request.headers["idempotency-key"] == "key-a"
        return httpx.Response(200, json=RECEIPT)
    result = execute(journal, handler)
    assert methods == ["POST", "GET", "POST"] and result["result"] == "CONFIRMED"
    assert [o["status"] for o in journal.snapshot()["outcomes"]] == [503, 404, 200]


def test_lost_commit_response_resolves_by_lookup_without_another_post(journal):
    methods = []
    async def handler(request):
        methods.append(request.method)
        if len(methods) == 1:
            raise httpx.ReadTimeout("redacted secret", request=request)
        return httpx.Response(200, json=RECEIPT)
    result = execute(journal, handler)
    assert methods == ["POST", "GET"] and result["result"] == "CONFIRMED"
    assert "redacted secret" not in json.dumps(journal.snapshot())


@pytest.mark.parametrize("status", [401,403,409,429,500,302])
def test_nonretryable_status_is_not_bypassed(journal, status):
    result = execute(journal, lambda req: httpx.Response(status))
    assert result["result"] == "REJECTED" and result["attempts"] == 1


def test_invalid_success_receipt_is_not_retried_or_confirmed(journal):
    result = execute(journal, lambda req: httpx.Response(200,json={**RECEIPT,"customer":"another"}))
    assert result["result"] == "REJECTED" and result["reason"] == "invalid_receipt"
    assert journal.snapshot()["outcomes"][0]["error"] == "invalid_receipt"


def test_attempt_limit_includes_every_lookup(journal):
    result = execute(journal, lambda req: httpx.Response(503), budget=CompletionBudget(max_attempts=3))
    assert result["result"] == "EXHAUSTED" and result["attempts"] == 3
    assert [a["kind"] for a in journal.snapshot()["attempts"]] == ["submit","lookup","lookup"]


def test_retry_after_beyond_deadline_does_not_trigger_an_early_retry(journal):
    result = execute(journal,lambda req:httpx.Response(503,headers={"retry-after":"60"}))
    assert result["result"] == "EXHAUSTED" and result["attempts"] == 1 and result["reason"] == "deadline"


def test_unsupported_retry_after_stops_instead_of_ignoring_server_wait(journal):
    result = execute(journal,lambda req:httpx.Response(503,headers={"retry-after":"tomorrow"}))
    assert result["result"] == "REJECTED" and result["reason"] == "unsupported_retry_after"


def test_lookup_wait_hint_is_honored_before_resubmission(journal):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(503) if len(calls)==1 else httpx.Response(404,headers={"retry-after":"60"})
    result = execute(journal,handler)
    assert calls == ["POST","GET"] and result["reason"] == "deadline"


def test_whole_network_attempt_is_cancelled_and_recorded_at_deadline(journal):
    cancelled = []
    async def handler(request):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    result = execute(journal,handler,budget=CompletionBudget(seconds=.05,attempt_seconds=.05))
    assert result["result"] == "EXHAUSTED" and result["attempts"] == 1
    assert cancelled and journal.snapshot()["outcomes"][0]["error"] == "transport_error"


def test_expired_budget_after_durable_recording_never_invokes_transport(journal, monkeypatch):
    begin = journal.begin
    def slow_begin(*args, **kwargs):
        value = begin(*args, **kwargs)
        time.sleep(.07)
        return value
    monkeypatch.setattr(journal,"begin",slow_begin)
    def forbidden(request):
        pytest.fail("Expired budget reached network transport")
    events = []
    result = execute(journal,forbidden,budget=CompletionBudget(seconds=.05,attempt_seconds=.05),events=events)
    assert result["result"]=="EXHAUSTED" and result["transport_calls"]==0
    assert events[0]["transport_invoked"] is False


@pytest.mark.parametrize("change", [{"seconds":float("nan")},{"seconds":float("inf")},{"seconds":True},
    {"max_attempts":0},{"max_attempts":17},{"max_attempts":True},{"seconds":31},{"max_backoff_seconds":6}])
def test_invalid_budget_refused(change):
    with pytest.raises(ValueError):
        CompletionBudget(**change)
