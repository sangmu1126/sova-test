"""Opt-in component acceptance on the owned personal ledger; no app migration.

Each case uses a fresh synthetic tenant/key scope in the REAL VM database.
These are atomicity/RLS tests, not enterprise identity or LIVE qualification.
Append-only test history is retained, not deleted or rewritten.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
from sova.adapters.postgres_registrations import PostgresRegistrations
from sova.adapters.registration.registry_codec import encode
from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.domain.capability_catalog import EnvironmentProfile
from sova.domain.change_impact import ChangeKind
from sova.adapters.postgres_registry_checkpoint import PostgresRegistryCheckpoint
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
    def connect():
        return psycopg.connect(dsn, connect_timeout=5)
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


def approve(store, request_id, *, approval_id, actor):
    return store.approve(request_id, approval_id=approval_id, actor=actor,
                         expected_digest=store.get(request_id)["request_digest"])


def test_no_publication_before_approval_and_receipt_retry_never_republishes(ledger):
    _, _, _, store, registry = ledger
    value = request(ledger)
    store.create(value)
    with pytest.raises(RecordNotFound):
        registry.resolve("test-verifier", "key-1")
    with pytest.raises(ContractError, match="Independent"):
        approve(store, value.request_id, approval_id="approval-1", actor=value.requested_by)
    first = approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
    assert first == approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
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
        approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
    assert counts(ledger) == [1, 0, 0]


def test_failure_after_registry_publish_rolls_back_publication_and_receipt(ledger, monkeypatch):
    connect, tenant, application, store, registry = ledger
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    def events():
        return feed.events(application_id=application, environments=("sova-contract-registry",), since=datetime(2020, 1, 1, tzinfo=UTC))
    value = request(ledger)
    store.create(value)
    original = PostgresVerifierRegistry.publish

    def lose_response(self, change):
        original(self, change)
        raise RuntimeError("Injected failure before approval receipt")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresVerifierRegistry, "publish", lose_response)
        with pytest.raises(RuntimeError, match="Injected"):
            approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
    with pytest.raises(RecordNotFound):
        registry.resolve("test-verifier", "key-1")
    assert counts(ledger) == [1, 0, 0]
    assert events() == ()
    approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
    assert counts(ledger) == [1, 1, 1]
    assert len(events()) == 1 and events()[0].kind == ChangeKind.VERIFIER


def test_two_competing_requests_cannot_both_advance_the_same_revision(ledger):
    _, _, _, store, _ = ledger
    for name in ("request-a", "request-b"):
        store.create(request(ledger, request_id=name))
    barrier = threading.Barrier(2)

    def concurrent_approve(name):
        barrier.wait(timeout=5)
        try:
            approve(store, name, approval_id="approve-" + name, actor="test-approver")
            return "recorded"
        except RepositoryConflict:
            return "revision-conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(concurrent_approve, ("request-a", "request-b")))
    assert sorted(values) == ["recorded", "revision-conflict"]
    assert counts(ledger) == [2, 1, 1]


def test_reused_approval_id_rolls_back_the_second_key_publication(ledger):
    _, _, _, store, registry = ledger
    for name in ("a", "b"):
        store.create(request(ledger, request_id="request-" + name, key="key-" + name))
    approve(store, "request-a", approval_id="single-approval-id", actor="test-approver")
    with pytest.raises(RepositoryConflict):
        approve(store, "request-b", approval_id="single-approval-id", actor="test-approver")
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
            approve(other, "request-1", approval_id="approval-1", actor="test-approver")
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


def profile():
    return EnvironmentProfile("test-profile", 1, "aws", "ec2", "postgresql", "scram", "private", "arm64", (("postgresql", "17.12"),))


def test_registration_and_change_are_atomic_without_claiming_feed_completeness(ledger):
    connect, tenant, application, _, _ = ledger
    registry = PostgresRegistrations(connect, tenant_id=tenant)
    value = profile()
    first = registry.register("environment_profile", value.profile_id, encode(value), expected_revision=0, registered_by="test-registrar")
    assert registry.register("environment_profile", value.profile_id, encode(value), expected_revision=0, registered_by="test-registrar") == first
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    events = feed.events(application_id=application, environments=("sova-contract-registry",), since=datetime(2020, 1, 1, tzinfo=UTC))
    assert len(events) == 1
    assert (events[0].kind, events[0].subject, events[0].digest) == (ChangeKind.ENVIRONMENT_PROFILE, value.profile_id, value.fingerprint)
    assert events[0].observed_at == first.registered_at
    assert feed.watermarks(("sova-contract-registry",)) == ()
    other = PostgresChangeFeed(connect, tenant_id="another-tenant")
    assert other.events(application_id=application, environments=("sova-contract-registry",), since=datetime(2020, 1, 1, tzinfo=UTC)) == ()


def test_registration_rollback_cannot_leave_a_change_event_behind(ledger):
    connect, tenant, _, _, _ = ledger
    with connect() as connection:
        @contextmanager
        def borrowed():
            yield connection

        registry = PostgresRegistrations(borrowed, tenant_id=tenant)
        with connection.transaction(force_rollback=True):
            value = profile()
            registry.register("environment_profile", value.profile_id, encode(value), expected_revision=0, registered_by="test-registrar")
            assert connection.execute("SELECT count(*) FROM sova_control.change_events").fetchone() == (1,)
    with connect() as conn:
        conn.execute("SELECT set_config('sova.tenant_id',%s,true)", (tenant,))
        for table in ("registrations", "registration_heads", "change_events"):
            assert conn.execute("SELECT count(*) FROM sova_control." + table).fetchone() == (0,)


def test_approved_key_emits_one_unambiguous_change_and_retry_does_not_duplicate_it(ledger):
    connect, tenant, application, store, _ = ledger
    value = request(ledger)
    store.create(value)
    approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
    approve(store, value.request_id, approval_id="approval-1", actor="test-approver")
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    events = feed.events(application_id=application, environments=("sova-contract-registry",), since=datetime(2020, 1, 1, tzinfo=UTC))
    assert len(events) == 1 and events[0].kind == ChangeKind.VERIFIER
    assert events[0].subject is None and events[0].environment_id is None
    assert feed.watermarks(("sova-contract-registry",)) == ()


def test_registry_checkpoint_rejects_precollection_baseline_and_marks_no_other_environment(ledger):
    connect, tenant, _, _, _ = ledger
    collector = PostgresRegistryCheckpoint(connect, tenant_id=tenant)
    with pytest.raises(ContractError, match="predates"):
        collector.checkpoint(baseline_observed_at=datetime(2020, 1, 1, tzinfo=UTC))
    with connect() as conn:
        baseline = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    result = collector.checkpoint(baseline_observed_at=baseline)
    assert result["external_environment_coverage"] is False
    marks = PostgresChangeFeed(connect, tenant_id=tenant).watermarks(("sova-contract-registry", "source", "target", "edge", "sova-control-plane"))
    assert len(marks) == 1 and marks[0].environment_id == "sova-contract-registry"


def test_checkpoint_cannot_pass_an_uncommitted_registry_writer(ledger):
    connect, tenant, application, _, _ = ledger
    started, release = threading.Event(), threading.Event()

    def writer():
        with connect() as connection:
            @contextmanager
            def borrowed():
                yield connection

            with connection.transaction():
                registry = PostgresRegistrations(borrowed, tenant_id=tenant)
                value = profile()
                registry.register("environment_profile", value.profile_id, encode(value), expected_revision=0, registered_by="test-registrar")
                started.set()
                assert release.wait(timeout=15), "Test writer release missing"

    with connect() as conn:
        baseline = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        try:
            assert started.wait(timeout=5)
            with pytest.raises(RepositoryConflict, match="in progress"):
                PostgresRegistryCheckpoint(connect, tenant_id=tenant).checkpoint(baseline_observed_at=baseline)
            assert PostgresChangeFeed(connect, tenant_id=tenant).watermarks(("sova-contract-registry",)) == ()
        finally:
            release.set()
        future.result(timeout=10)
    PostgresRegistryCheckpoint(connect, tenant_id=tenant).checkpoint(baseline_observed_at=baseline)
    events = PostgresChangeFeed(connect, tenant_id=tenant).events(application_id=application,
        environments=("sova-contract-registry",), since=baseline)
    assert len(events) == 1


def test_runtime_cannot_rewind_a_registry_head_or_commit_one_without_history(ledger):
    connect, tenant, _, _, _ = ledger
    registry = PostgresRegistrations(connect, tenant_id=tenant)
    value = profile()
    registry.register("environment_profile", value.profile_id, encode(value), expected_revision=0, registered_by="test-registrar")
    with pytest.raises(psycopg.errors.RaiseException, match="advance exactly"):
        with registry.transaction() as conn:
            conn.execute("UPDATE sova_control.registration_heads SET revision=revision WHERE subject_id=%s", (value.profile_id,))
    with pytest.raises(psycopg.errors.RaiseException, match="immutable history"):
        with registry.transaction() as conn:
            conn.execute("UPDATE sova_control.registration_heads SET revision=revision+1 WHERE subject_id=%s", (value.profile_id,))
    assert registry.current("environment_profile", value.profile_id).revision == 1
    assert len(registry.history("environment_profile", value.profile_id)) == 1


def test_approval_cannot_publish_a_key_other_than_the_reviewed_request_digest(ledger):
    _, _, _, store, _ = ledger
    store.create(request(ledger))
    with pytest.raises(RepositoryConflict, match="review"):
        store.approve("request-1", approval_id="approval-1", actor="test-approver", expected_digest="0" * 64)
    assert counts(ledger) == [1, 0, 0]
