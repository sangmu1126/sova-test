"""Opt-in component acceptance on the owned personal ledger; no app migration.

Each case uses a fresh synthetic tenant/key scope in the REAL VM database.
These are atomicity/RLS tests, not enterprise identity or LIVE qualification.
Append-only test history is retained, not deleted or rewritten.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import psycopg
import pytest

from sova.adapters.postgres_trust_requests import PostgresTrustRequests
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.trust_requests import VerifierTrustRequest
from sova.ports.plans import RepositoryConflict, RecordNotFound

pytestmark = pytest.mark.skipif(os.environ.get("SOVA_TEST_PERSONAL_TRUST") != "1", reason="Explicit owned personal ledger opt-in")


@pytest.fixture
def ledger():
    file = Path(os.environ["SOVA_PERSONAL_CONTROL_RUNTIME_DSN_FILE"])
    assert file.is_absolute() and file.stat().st_mode & 0o077 == 0
    dsn = file.read_text()
    connect = lambda: psycopg.connect(dsn, connect_timeout=5)
    with connect() as conn:
        assert conn.execute("SELECT current_database(),current_user").fetchone() == (
            "sj_control_b0be270e62", "sj_runtime_b0be270e62")
        assert conn.info.get_parameters()["sslmode"] == "verify-full"
    tenant, application = "trust-test-" + uuid4().hex, "test-orders"
    store = PostgresTrustRequests(connect, tenant_id=tenant, application_id=application)
    registry = PostgresVerifierRegistry(connect, tenant_id=tenant, application_id=application)
    return connect, tenant, application, store, registry


def request(ledger, *, request_id="request-1", issuer="test-verifier", key="key-1", expected=0):
    _, tenant, application, _, _ = ledger
    now = datetime.now(UTC)
    trust = VerifierTrust(issuer, key, "ab" * 32,
        (ResourceScope(tenant, application, "qualification-test-only", "unit"),),
        ("test-verifier-1",), now - timedelta(minutes=1), now + timedelta(hours=1))
    return VerifierTrustRequest(tenant, application, request_id, expected, trust, "test-requester",
        "Isolated component acceptance, not production trust", now, now + timedelta(minutes=15))


def counts(ledger):
    connect, tenant, application, _, _ = ledger
    with connect() as conn:
        conn.execute("SELECT set_config('sova.tenant_id',%s,true)", (tenant,))
        return [conn.execute("SELECT count(*) FROM sova_control." + table + " WHERE application_id=%s",
            (application,)).fetchone()[0] for table in ("verifier_requests", "verifier_request_approvals", "verifier_trust_events")]


def test_no_publication_before_approval_and_receipt_retry_never_republishes(ledger):
    _, _, _, store, registry = ledger
    value = request(ledger)
    store.create(value)
    with pytest.raises(RecordNotFound):
        registry.resolve("test-verifier", "key-1")
    with pytest.raises(ContractError, match="Independent"):
        store.approve(value.request_id, approval_id="approval-1", actor=value.requested_by)
    first = store.approve(value.request_id, approval_id="approval-1", actor="test-approver")
    assert first == store.approve(value.request_id, approval_id="approval-1", actor="test-approver")
    assert store.get(value.request_id)["approval"] == first
    assert registry.resolve("test-verifier", "key-1") == value.trust
    assert counts(ledger) == [1, 1, 1]


def test_lost_response_retry_preserves_request_expiration_and_actor(ledger):
    _, _, _, store, _ = ledger
    value = request(ledger)
    first = store.create(value)
    retry = replace(value, requested_at=value.requested_at + timedelta(seconds=1), expires_at=value.expires_at + timedelta(seconds=1))
    assert store.create(retry) == first
    with pytest.raises(RepositoryConflict):
        store.create(replace(retry, requested_by="other-actor"))
    assert counts(ledger) == [1, 0, 0]


def test_expired_request_blocks_even_when_proposed_trust_is_still_current(ledger):
    _, _, _, store, _ = ledger
    value = request(ledger)
    value = replace(value, expires_at=value.requested_at + timedelta(seconds=2))
    store.create(value)
    time.sleep(2.1)
    with pytest.raises(ContractError, match="expired"):
        store.approve(value.request_id, approval_id="approval-1", actor="test-approver")
    assert counts(ledger) == [1, 0, 0]


def test_failure_after_registry_publish_rolls_back_publication_and_receipt(ledger, monkeypatch):
    _, _, _, store, registry = ledger
    value = request(ledger)
    store.create(value)
    original = PostgresVerifierRegistry.publish

    def lose_response(self, change):
        original(self, change)
        raise RuntimeError("Injected failure before approval receipt")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresVerifierRegistry, "publish", lose_response)
        with pytest.raises(RuntimeError, match="Injected"):
            store.approve(value.request_id, approval_id="approval-1", actor="test-approver")
    with pytest.raises(RecordNotFound):
        registry.resolve("test-verifier", "key-1")
    assert counts(ledger) == [1, 0, 0]
    store.approve(value.request_id, approval_id="approval-1", actor="test-approver")
    assert counts(ledger) == [1, 1, 1]


def test_two_competing_requests_cannot_both_advance_the_same_revision(ledger):
    _, _, _, store, _ = ledger
    for name in ("request-a", "request-b"):
        store.create(request(ledger, request_id=name))
    barrier = threading.Barrier(2)

    def approve(name):
        barrier.wait(timeout=5)
        try:
            store.approve(name, approval_id="approve-" + name, actor="test-approver")
            return "recorded"
        except RepositoryConflict:
            return "revision-conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(approve, ("request-a", "request-b")))
    assert sorted(values) == ["recorded", "revision-conflict"]
    assert counts(ledger) == [2, 1, 1]


def test_reused_approval_id_rolls_back_the_second_key_publication(ledger):
    _, _, _, store, registry = ledger
    for name in ("a", "b"):
        store.create(request(ledger, request_id="request-" + name, key="key-" + name))
    store.approve("request-a", approval_id="single-approval-id", actor="test-approver")
    with pytest.raises(RepositoryConflict):
        store.approve("request-b", approval_id="single-approval-id", actor="test-approver")
    with pytest.raises(RecordNotFound):
        registry.resolve("test-verifier", "key-b")
    assert counts(ledger) == [2, 1, 1]


def test_other_tenant_and_other_application_cannot_read_or_approve(ledger):
    connect, tenant, application, store, _ = ledger
    store.create(request(ledger))
    for other in (PostgresTrustRequests(connect, tenant_id="other-tenant", application_id=application),
                  PostgresTrustRequests(connect, tenant_id=tenant, application_id="other-app")):
        with pytest.raises(RecordNotFound):
            other.get("request-1")
        with pytest.raises(RecordNotFound):
            other.approve("request-1", approval_id="approval-1", actor="test-approver")
    with connect() as conn:
        conn.execute("SELECT set_config('sova.tenant_id',%s,true)", ("other-tenant",))
        assert conn.execute("SELECT count(*) FROM sova_control.verifier_requests").fetchone()[0] == 0
    assert counts(ledger) == [1, 0, 0]


def test_runtime_cannot_delete_or_rewrite_request_history(ledger):
    connect, _, _, store, _ = ledger
    store.create(request(ledger))
    with connect() as conn:
        for table in ("verifier_requests", "verifier_request_approvals"):
            for statement in ("DELETE FROM sova_control." + table + " WHERE FALSE",
                              "UPDATE sova_control." + table + " SET digest=digest WHERE FALSE"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction(force_rollback=True):
                        conn.execute(statement)
