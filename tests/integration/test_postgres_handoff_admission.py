"""Approved handoff admission over the real lifecycle and claim stores.

Synthetic contracts and local Ed25519 keys; no live environment is changed.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from uuid import uuid4

from contract_world import build_world
from handoff_support import complete_feeds
import psycopg
import pytest

from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_handoff_claims import PostgresHandoffClaims
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.application.change_monitor import ChangeImpactSource
from sova.application.handoff_admission import HandoffAdmission
from sova.application.service_contracts import example_service_contract
from sova.domain.change_impact import ChangeEvent, ChangeKind, PlanReevaluationRequired
from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose
from sova.domain.service_contracts import ServiceContract
from sova.ports.plans import RepositoryConflict

pytestmark = pytest.mark.postgres

VERIFIER = "independent-verifier-1"


@pytest.fixture
def stores(postgres_database):
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex

    def connect():
        return psycopg.connect(dsn, user=role)

    lifecycle = PostgresLifecycleRepository(
        connect, tenant_id=tenant, clock=lambda: datetime.now(UTC)
    )
    claims = PostgresHandoffClaims(connect, tenant_id=tenant)
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    compiled = PostgresCompiledPlans(connect, tenant_id=tenant)
    admission = HandoffAdmission(
        lifecycle=lifecycle, claims=claims,
        changes=ChangeImpactSource(feed=feed, clock=lambda: datetime.now(UTC)),
        compiled=compiled, clock=lambda: datetime.now(UTC),
    )
    return dict(tenant=tenant, lifecycle=lifecycle, claims=claims, admission=admission,
                feed=feed, compiled=compiled, dsn=dsn)


def prepared(stores, now, service, *, uid_prefix=None, feeds=True):
    """Compile, lower, store, and approve one synthetic handoff for ``service``."""
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = stores["tenant"]
    data["metadata"]["id"] = service
    world = build_world(now, contract=ServiceContract.from_document(data))
    if uid_prefix:
        world["snapshot"] = replace(
            world["snapshot"],
            resource_uids=tuple(
                (n, uid_prefix + u) for n, u in world["snapshot"].resource_uids
            ),
        )
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)

    def recompile():
        return compile_revision(
            world["contract"],
            world["snapshot"],
            requested_nodes=("api-a", "api-b", "database"),
            expected_tenant=stores["tenant"],
            source=world["source"],
            target=world["target"],
            manifest=world["manifest"],
            qualification=world["qualification"],
            rows=2000,
            authenticate=world["auth"],
            clock=lambda: datetime.now(UTC),
        )

    revision, result = recompile()
    assert result["status"] == "COMPILED", result["blockers"]
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    verifiers = {k: VERIFIER for k in kinds}
    plan = lower_plan(revision, created_by="planner", verifiers=verifiers)
    stores["lifecycle"].save_plan(plan)
    if feeds:
        complete_feeds(stores["feed"], revision, plan)
    approval_id = "approval-" + uuid4().hex[:12]
    stores["lifecycle"].save_approval(
        service,
        plan.plan_id,
        approval_id,
        PlanApproval(
            plan.fingerprint,
            "security-approver",
            PlanPurpose.MIGRATION_PHASE,
            now + timedelta(minutes=3),
        ),
    )
    return dict(world=world, plan=plan, approval_id=approval_id,
                recompile=recompile, verifiers=verifiers, service=service)


def admit(stores, item):
    return stores["admission"].admit(
        item["service"], item["approval_id"],
        recompile=item["recompile"], verifiers=item["verifiers"],
    )


def test_admission_records_claims_and_blocks_a_plan_sharing_the_database(stores, now):
    orders = prepared(stores, now, "orders-service")
    billing = prepared(stores, now, "billing-service")
    value = admit(stores, orders)
    assert value["status"] == "ADMITTED"
    assert value["authorizes_execution"] is False and value["commands_issued"] is False
    active = stores["claims"].active()
    assert ("source", "synthetic-source", "synthetic-uid-database") in {
        key for _, _, key, _ in active
    }
    with pytest.raises(RepositoryConflict, match="Overlapping"):
        admit(stores, billing)
    stores["claims"].release(
        application_id="orders-service",
        plan_digest=orders["plan"].fingerprint,
        actor_id="handoff-coordinator",
    )
    assert admit(stores, billing)["status"] == "ADMITTED"


def test_disjoint_plans_are_admitted_together(stores, now):
    orders = prepared(stores, now, "orders-service")
    billing = prepared(stores, now, "billing-service", uid_prefix="billing-")
    assert admit(stores, orders)["status"] == "ADMITTED"
    assert admit(stores, billing)["status"] == "ADMITTED"


def test_a_plan_is_admitted_once_and_never_again_after_release(stores, now):
    orders = prepared(stores, now, "orders-service")
    admit(stores, orders)
    with pytest.raises(RepositoryConflict, match="already admitted"):
        admit(stores, orders)
    stores["claims"].release(
        application_id="orders-service",
        plan_digest=orders["plan"].fingerprint,
        actor_id="handoff-coordinator",
    )
    with pytest.raises(RepositoryConflict, match="at most once"):
        admit(stores, orders)


def test_revoked_approval_or_changed_inputs_are_not_admitted(stores, now):
    orders = prepared(stores, now, "orders-service")
    stored = stores["lifecycle"].get_approval("orders-service", orders["approval_id"])
    stores["lifecycle"].revoke_approval(
        "orders-service", orders["approval_id"],
        expected_revision=stored.revision, actor_id="security-approver",
    )
    with pytest.raises(ContractError, match="revoked"):
        admit(stores, orders)
    billing = prepared(stores, now, "billing-service", uid_prefix="billing-")
    key = (billing["world"]["trust"].issuer_id, billing["world"]["trust"].key_id)
    billing["world"]["trusts"][key] = replace(billing["world"]["trust"], revoked=True)
    with pytest.raises(ContractError, match="no longer compilable"):
        admit(stores, billing)
    assert stores["claims"].active() == ()


def test_concurrent_conflicting_admissions_admit_exactly_one(stores, now):
    orders = prepared(stores, now, "orders-service")
    billing = prepared(stores, now, "billing-service")
    start = Barrier(2)

    def race(item):
        start.wait()
        try:
            return admit(stores, item)["status"]
        except RepositoryConflict:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(race, (orders, billing)))
    assert outcomes == ["ADMITTED", "CONFLICT"]
    assert len({plan for _, plan, _, _ in stores["claims"].active()}) == 1


def test_active_security_hold_blocks_admission(stores, now):
    from sova.adapters.postgres_governance import PostgresSecurityHolds
    from sova.domain.contracts import ResourceScope
    from sova.domain.governance import SecurityHold

    orders = prepared(stores, now, "orders-service")
    connect = stores["claims"].connect
    PostgresSecurityHolds(connect, tenant_id=stores["tenant"]).open_hold(SecurityHold(
        "incident-1", ResourceScope(stores["tenant"], "orders-service", "aws-source", "db"),
        "incident-commander", "Suspected compromise", datetime.now(UTC)))
    with pytest.raises(RepositoryConflict, match="security hold"):
        admit(stores, orders)
    assert stores["claims"].active() == ()


def test_unit_change_after_the_snapshot_or_a_feed_gap_blocks_admission(stores, now):
    """INV-08: a recreated source database or an unreported feed is never "unchanged"."""
    orders = prepared(stores, now, "orders-service")
    stores["feed"].record(ChangeEvent(
        "ev-db-recreated", stores["tenant"], "another-service", ChangeKind.RESOURCE,
        "synthetic-source", "synthetic-uid-database", None,
        datetime.now(UTC) - timedelta(milliseconds=1)), collector_id="change-collector-1")
    with pytest.raises(PlanReevaluationRequired, match="UNIT_RESOURCE_CHANGED:database"):
        admit(stores, orders)
    assert stores["claims"].active() == ()


def test_missing_change_feeds_block_admission(stores, now):
    orders = prepared(stores, now, "orders-service", feeds=False)
    with pytest.raises(PlanReevaluationRequired, match="CHANGE_FEED_INCOMPLETE"):
        admit(stores, orders)
    assert stores["claims"].active() == ()


def test_admission_stores_the_exact_admitted_plan(stores, now):
    """After a restart the admitted plan is loaded by digest, not recompiled."""
    orders = prepared(stores, now, "orders-service")
    value = admit(stores, orders)
    loaded = stores["compiled"].for_plan(orders["plan"])
    assert loaded.digest == value["compiled_digest"] == orders["plan"].origin_plan_digest
    assert stores["compiled"].save(loaded) == loaded.digest  # idempotent
    with psycopg.connect(stores["dsn"]) as admin:  # tamper with the stored row
        admin.execute(
            "UPDATE sova_control.compiled_plans SET canonical=replace(canonical,"
            "'offline-copy','offline-c0py') WHERE tenant_id=%s", (stores["tenant"],))
    with pytest.raises(ContractError, match="does not match its digest"):
        stores["compiled"].load(loaded.digest)
    other = PostgresCompiledPlans(stores["claims"].connect, tenant_id="tenant-other")
    with pytest.raises(ContractError, match="not found"):
        other.load(loaded.digest)
