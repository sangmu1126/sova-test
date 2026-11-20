"""Real native PG policy/implementation impact; keys, actors and inventory are fixtures."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import time
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_service_control_plane import service as service  # noqa: F401
from test_service_control_plane import register_everything, request_of, feeds_complete

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner, Ed25519EvidenceVerifier
from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service import schema
from sova.service.control_plane import Role

pytestmark = pytest.mark.postgres


def db_time(connect):
    with connect() as conn:
        at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    # Observe actual DB time; never backdate events or modify either clock.
    until = time.monotonic() + 2
    while datetime.now(UTC) < at:
        if time.monotonic() >= until:
            raise RuntimeError("Test host has not reached the observed DB clock")
        time.sleep(0.001)
    return at


@pytest.fixture
def planned(service):
    s = service
    register_everything(s)
    compiled = s["plane"].compile(s["people"][Role.PLANNER], request_of(s["world"]))
    s["plane"].approve(s["people"][Role.APPROVER], s["app"], compiled["plan_id"], "approval-grants")
    plan = s["plane"]._lifecycle(s["tenant"]).get_plan(s["app"], compiled["plan_id"])
    signer = Ed25519EvidenceSigner(Ed25519PrivateKey.generate(), issuer_id="grant-test", key_id="k1")
    at = db_time(s["connect"])
    trust = VerifierTrust(signer.issuer_id, signer.key_id, signer.public_key_hex,
        tuple(sorted({o.scope for o in plan.obligations}, key=lambda scope: (scope.environment_id, scope.resource_id))),
        ("handoff-verifier-1",), at - timedelta(minutes=1), at + timedelta(minutes=10))
    change = VerifierTrustChange(s["tenant"], s["app"], 1, trust, "security-requester",
        "independent-approver", "Test only: grant fixed unit verifier", at, at + timedelta(minutes=1))
    registry = PostgresVerifierRegistry(s["connect"], tenant_id=s["tenant"], application_id=s["app"])
    return {**s, "compiled": compiled, "plan": plan, "signer": signer,
            "change": change, "registry": registry}


def event_for(s, change):
    feed = PostgresChangeFeed(s["connect"], tenant_id=s["tenant"])
    with feed.transaction() as conn:
        row = conn.execute("""SELECT kind,observed_at,collector_id FROM sova_control.change_events
            WHERE tenant_id=%s AND event_id=%s""", (s["tenant"], "verifier-" + change.fingerprint)).fetchone()
    return row


def impact(s):
    revision = PostgresCompiledPlans(s["connect"], tenant_id=s["tenant"]).for_plan(s["plan"])
    return s["plane"]._changes(s["tenant"])(revision, s["plan"], issued=0)


def test_initial_key_grant_after_approval_never_claims_feed_completeness(planned):
    s = planned
    s["registry"].publish(s["change"])
    assert event_for(s, s["change"])[::2] == ("policy", "registry-trust-grant-outbox-v1")
    before_marks = impact(s)
    assert before_marks.plan_unknown and not before_marks.commands_allowed
    feeds_complete(s, s["compiled"])  # Explicit synthetic feed fixture, not the publisher.
    observed = impact(s)
    assert not observed.plan_stale and observed.commands_allowed
    assert all(not observed.evidence_counts(o.obligation_id, s["change"].approved_at)
               for o in s["plan"].obligations)
    assert s["plane"]._lifecycle(s["tenant"]).get_plan(s["app"], s["compiled"]["plan_id"]) == s["plan"]


def test_scope_only_grant_invalidates_old_pass_and_revocation_still_blocks(planned):
    s = planned
    s["registry"].publish(s["change"])
    feeds_complete(s, s["compiled"])
    obligation = s["plan"].obligations[0]
    at = db_time(s["connect"])
    record = EvidenceRecord("grant-pass-1", obligation.obligation_id, obligation.scope,
        s["plan"].revisions, "handoff-verifier-1", at, at + timedelta(minutes=1),
        VerificationResult.PASS, "Synthetic policy regression", "a" * 64)
    authority = SignedEvidenceAuthority(PostgresSignedEvidence(s["connect"], tenant_id=s["tenant"]),
        Ed25519EvidenceVerifier(s["registry"].resolve), clock=lambda: datetime.now(UTC))
    authority.ingest(s["signer"].sign(record, issued_at=at))
    assert authority.authenticate(record) == record
    assert impact(s).evidence_counts(record.obligation_id, at)
    extra = ResourceScope(s["tenant"], s["app"], "another-fixture-env", "another-fixture-unit")
    expansion = replace(s["change"], revision=2, trust=replace(s["change"].trust,
        allowed_scopes=(*s["change"].trust.allowed_scopes, extra)))
    s["registry"].publish(expansion)
    assert event_for(s, expansion)[0] == "policy"
    result = impact(s)
    assert result.commands_allowed and not result.evidence_counts(record.obligation_id, at)
    # A valid signature is deliberately insufficient after the policy change.
    assert authority.authenticate(record) == record
    fresh_at = db_time(s["connect"])
    fresh = replace(record, evidence_id="grant-pass-fresh", observed_at=fresh_at,
                    valid_until=fresh_at+timedelta(minutes=1))
    authority.ingest(s["signer"].sign(fresh, issued_at=fresh_at))
    assert impact(s).evidence_counts(fresh.obligation_id, fresh.observed_at)
    revoked = replace(expansion, revision=3, trust=replace(expansion.trust, revoked=True))
    s["registry"].publish(revoked)
    assert event_for(s, revoked)[0] == "verifier"
    assert not impact(s).commands_allowed
    with pytest.raises(ContractError, match="revoked"):
        authority.authenticate(record)


@pytest.mark.parametrize("mutation", ["scope-removal", "verifier-version", "valid-from", "valid-until", "valid-until-shortened", "revoke"])
def test_non_additive_changes_keep_conservative_plan_block(planned, mutation):
    s = planned
    s["registry"].publish(s["change"])
    trust = s["change"].trust
    if mutation == "scope-removal":
        assert len(trust.allowed_scopes) > 1
        changed = replace(trust, allowed_scopes=trust.allowed_scopes[1:])
    elif mutation == "verifier-version":
        changed = replace(trust, allowed_verifiers=("different-version",))
    elif mutation == "valid-from":
        changed = replace(trust, valid_from=trust.valid_from + timedelta(seconds=1))
    elif mutation == "valid-until":
        changed = replace(trust, valid_until=trust.valid_until + timedelta(seconds=1))
    elif mutation == "valid-until-shortened":
        changed = replace(trust, valid_until=trust.valid_until - timedelta(seconds=1))
    else:
        changed = replace(trust, revoked=True)
    change = replace(s["change"], revision=2, trust=changed)
    s["registry"].publish(change)
    feeds_complete(s, s["compiled"])
    assert event_for(s, change)[0] == "verifier"
    assert impact(s).plan_stale and not impact(s).commands_allowed


def test_actual_verifier_implementation_change_still_blocks(planned):
    s = planned
    s["registry"].publish(s["change"])
    PostgresChangeFeed(s["connect"], tenant_id=s["tenant"]).record(ChangeEvent(
        "actual-verifier-implementation-change", s["tenant"], s["app"], ChangeKind.VERIFIER,
        None, "registry-attestor-1", None, db_time(s["connect"])), collector_id="fixture-code-collector")
    feeds_complete(s, s["compiled"])
    assert impact(s).plan_stale and not impact(s).commands_allowed


@pytest.mark.parametrize("mismatch", ["row-tenant", "scope-app", "document-version"])
def test_unclassifiable_trust_rows_never_gain_the_additive_exception(planned, mismatch):
    from psycopg.types.json import Jsonb

    s = planned
    document = s["change"].document
    if mismatch == "row-tenant":
        document["tenant_id"] = "unrelated-tenant"
    elif mismatch == "scope-app":
        document["trust"]["allowed_scopes"][0]["application_id"] = "unrelated-app"
    else:
        document["schema_version"] = 99
    # Bypass only the test's typed publisher to inject an invalid persistent
    # row as the already-scoped runtime role. The DB trigger must stay strict.
    feed = PostgresChangeFeed(s["connect"], tenant_id=s["tenant"])
    with feed.transaction() as conn:
        conn.execute("""INSERT INTO sova_control.verifier_trust_events
            (tenant_id,application_id,issuer_id,key_id,revision,digest,document)
            VALUES (%s,%s,%s,%s,1,%s,%s)""", (s["tenant"],s["app"],
            s["signer"].issuer_id,s["signer"].key_id,"e"*64,Jsonb(document)))
        assert conn.execute("SELECT kind FROM sova_control.change_events WHERE event_id=%s",
                            ("verifier-"+"e"*64,)).fetchone() == ("verifier",)
    feeds_complete(s, s["compiled"])
    assert impact(s).plan_stale and not impact(s).commands_allowed


def test_upgrade_preserves_old_events_and_rejects_runtime_migration(postgres_database):
    owner_dsn, role = postgres_database
    name = "trust_impact_upgrade_" + uuid4().hex
    with psycopg.connect(owner_dsn, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(owner_dsn, dbname=name)
    try:
        with psycopg.connect(dsn) as owner:
            with patch.object(schema, "MIGRATIONS", schema.MIGRATIONS[:-1]):
                schema.apply(owner)
            schema.grant_runtime(owner, role)
        connect = lambda: psycopg.connect(dsn, user=role)  # noqa: E731
        scope = ResourceScope("fixture-tenant", "fixture-app", "fixture-env", "fixture-unit")
        key = Ed25519EvidenceSigner(Ed25519PrivateKey.generate(), issuer_id="fixture-issuer", key_id="k1")
        at = db_time(connect)
        change = VerifierTrustChange(scope.tenant_id, scope.application_id, 1,
            VerifierTrust(key.issuer_id, key.key_id, key.public_key_hex, (scope,), ("v1",),
                at-timedelta(minutes=1), at+timedelta(minutes=5)),
            "requester", "independent-approver", "Upgrade fixture", at, at+timedelta(minutes=1))
        registry = PostgresVerifierRegistry(connect, tenant_id=scope.tenant_id, application_id=scope.application_id)
        registry.publish(change)
        with psycopg.connect(dsn) as owner:
            before = owner.execute("SELECT * FROM sova_control.change_events ORDER BY event_id").fetchall()
            migration_rows = dict(owner.execute("SELECT name,sha256 FROM sova_control.schema_migrations"))
            assert before[0][3] == "verifier"
            owner.commit()
            assert schema.apply(owner) == ["postgres_verifier_grant_impact_v1.sql"]
            assert owner.execute("SELECT * FROM sova_control.change_events ORDER BY event_id").fetchall() == before
            assert all(dict(owner.execute("SELECT name,sha256 FROM sova_control.schema_migrations"))[k] == v
                       for k,v in migration_rows.items())
            owner.commit()
            assert schema.apply(owner) == []
        new_key = replace(change, trust=replace(change.trust, key_id="k2"))
        registry.publish(new_key)
        with PostgresChangeFeed(connect, tenant_id=scope.tenant_id).transaction() as conn:
            assert conn.execute("SELECT kind FROM sova_control.change_events WHERE event_id=%s",
                ("verifier-"+new_key.fingerprint,)).fetchone() == ("policy",)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect() as conn:
                conn.execute(schema.text_of("postgres_verifier_grant_impact_v1.sql"))
    finally:
        with psycopg.connect(owner_dsn, autocommit=True) as owner:
            owner.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
