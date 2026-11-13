"""A compiled handoff that really moves data between two local PostgreSQL databases.

Everything runs against the disposable test cluster: a source and a target
database, an application role that writes orders, and independent probes that
measure each obligation. The control plane is unchanged: approval, admission,
authority record, next-step commands through the existing effect ledger, and
signed evidence decide every step. No cloud resource is touched.
"""

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from contract_world import build_world
from handoff_support import admitted_handoff, complete_feeds
import psycopg
from psycopg import sql
import pytest
from support import permit_for

from sova.adapters.postgres_effects import PostgresEffectJournal
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes, artifact_digest
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.application.effects import EffectExecutor, UncertainEffect
from sova.application.execution_recovery import ExecutionRecovery
from sova.application.handoff_admission import HandoffAdmission
from sova.domain.contracts import EffectStatus
from sova.domain.evidence import VerificationResult
from sova.domain.execution import QuiescenceProof, pending_digest
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.operations import OperationRequest
from sova.domain.plan_compiler import FAILBACK_STRATEGY, compile_failback
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose

pytestmark = pytest.mark.postgres
OPERATION = "real-handoff"
TABLES = (("orders", "id"), ("work_items", "id"), ("idempotency_results", "customer"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id bigint PRIMARY KEY, customer text NOT NULL,
  idempotency_key text NOT NULL, amount numeric NOT NULL, UNIQUE (customer, idempotency_key));
CREATE TABLE IF NOT EXISTS work_items (id bigint PRIMARY KEY, order_id bigint NOT NULL,
  state text NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_results (customer text NOT NULL,
  idempotency_key text NOT NULL, result text NOT NULL,
  PRIMARY KEY (customer, idempotency_key));
"""
PROBE_INSERT = ("INSERT INTO orders (id, customer, idempotency_key, amount) "
                "VALUES (-1, 'sova-probe', 'sova-probe', 0)")


class Route:
    def __init__(self, database):
        self.database = database

    def get(self):
        return self.database

    def set(self, database):
        self.database = database


@pytest.fixture
def business(postgres_database):
    """Source, target, scratch and another tenant's database, plus the app role."""
    dsn, _ = postgres_database
    suffix = uuid4().hex[:10]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    app = "orders_app_" + suffix
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(app)))
        for name in names.values():
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        for zoned in (names["tgt"], names["src"]):
            admin.execute(sql.SQL("COMMENT ON DATABASE {} IS 'approved-zone-a'").format(
                sql.Identifier(zoned)))
        admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(names["other"])))

    def connect(database):
        return psycopg.connect(dsn, dbname=database)

    def connect_as(database, role):
        return psycopg.connect(dsn, dbname=database, user=role)

    for name in (names["src"], names["tgt"], names["scratch"]):
        with connect(name) as conn:
            conn.execute(SCHEMA)
    with connect(names["src"]) as conn:
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                             "public TO {}").format(sql.Identifier(app)))
    with connect_as(names["src"], app) as conn:  # business before the handoff
        for i in range(1, 51):
            conn.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                         (i, f"customer-{i % 7}", f"key-{i}", i * 10))
            conn.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                         (f"customer-{i % 7}", f"key-{i}"))
            conn.execute("INSERT INTO work_items VALUES (%s, %s, 'done')", (i, i))
    yield dict(names=names, app=app, connect=connect, connect_as=connect_as, dsn=dsn)


class LosesResponse:
    """The effect happens, the executor never hears back (AT-20)."""

    def __init__(self, inner):
        self.inner, self.adapter_id = inner, inner.adapter_id

    def validate(self, command):
        self.inner.validate(command)

    def apply(self, command):
        self.inner.apply(command)
        raise ConnectionError("response lost after the change was made")

    def readback(self, command, now):
        return self.inner.readback(command, now)


def drive(h, revision, plan, *, adapter, probes, other_db, scratch_db, operation,
          approval_id, until=None, before=None, lose_response_at=(), events=None):
    """Run a compiled plan's steps through the ledger with measured evidence."""
    lifecycle = h["lifecycle"]
    approval = lifecycle.get_approval(h["app"], approval_id)
    PostgresOperationRepository(lifecycle).submit(OperationRequest(
        h["tenant"], h["app"], operation, plan.plan_id, plan.fingerprint,
        approval_id, approval.revision, "operator", operation + "-request"))
    ownership = PostgresExecutionOwnership(lifecycle)

    def executor_for(lease):
        journal = PostgresEffectJournal(ownership, lease)
        return journal, EffectExecutor(journal, authorize=journal.authorize,
                                       clock=lambda: datetime.now(UTC))

    lease = ownership.acquire(h["app"], operation, "worker")
    journal, engine = executor_for(lease)
    h["coordinator"].open(revision, plan, actor_id="coordinator")
    contract = h["world"]["contract"].document["spec"]
    doc = json.loads(revision.canonical)
    observed = {}  # step kind -> ledger observation time

    def measure(obligation):
        kind, oid = obligation["kind"], obligation["id"]
        if kind == "placement_evidence":
            return probes.zone("target", set(contract["residency"][oid.split(".", 1)[1]]))
        if kind == "return_target_check":
            return probes.write_denied("target")
        if oid == "fence.source_writes_denied" or kind == "stale_route_probe":
            return probes.write_denied("source")
        if kind == "final_content_comparison":
            return probes.content_equal(oid.split(".", 1)[1])
        if kind == "business_invariant":
            return probes.order_effects_once()
        if oid == "security.deny_cross_tenant_data_access":
            return probes.cross_tenant_denied(other_db)
        if oid == "security.verified_transport_encryption":
            return probes.transport_encrypted("target")
        if kind == "restore_drill" or oid == "security.tested_restore":
            return probes.restore_drill(scratch_db)
        if kind == "authority_readback":
            return probes.single_writer()
        if kind == "measured_budget":
            end = "activate_target" if oid.endswith("write_admission_pause") else "commit_route"
            spent = (observed[end] - observed["pause_admission"]).total_seconds() * 1000
            return spent <= obligation["budget_ms"], {
                "probe": "measured_budget", "spent_ms": round(spent, 3),
                "budget_ms": obligation["budget_ms"]}
        raise AssertionError(f"no probe for {oid}")

    obligations = {o["id"]: o for o in doc["obligations"]}
    record = None
    for op in doc["operations"]:
        if op["kind"] == until:
            break
        if before:
            before(op["kind"])
        effect = None
        if op["adapter"]:
            command = h["coordinator"].next_command(revision, plan, operation_id=operation,
                                                    epoch=lease.epoch)
            if op["kind"] in lose_response_at:
                with pytest.raises(UncertainEffect):
                    engine.execute(command, permit_for(command), LosesResponse(adapter))
                held = h["coordinator"].advance_from_ledger(
                    revision, plan, effect=journal.get(command), actor_id="coordinator")
                events.append((op["kind"], str(held.state), held.reason,
                               held.activation_possible))
                # The executor that lost the response is gone; its lease ends.
                with lifecycle.transaction() as conn:
                    conn.execute(
                        "UPDATE sova_control.app_execution SET valid_until="
                        "clock_timestamp()-interval '1 second' WHERE application_id=%s",
                        (h["app"],))
                    at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                    pending = ownership.pending(conn, h["app"])
                # Recovery lease bound to exactly the pending effect (existing rule).
                recovery = ExecutionRecovery(ownership, authenticate_proof=lambda _: None)\
                    .acquire(QuiescenceProof(h["tenant"], h["app"], operation, lease.epoch,
                                             pending_digest(pending), "e" * 64, at,
                                             at + timedelta(minutes=1)), "recovery-worker")
                # Read-only reconciliation: look at the databases again, never re-run.
                effect = executor_for(recovery)[1].reconcile(command, permit_for(command),
                                                             adapter)
                ownership.release(recovery)
                lease = ownership.acquire(h["app"], operation, "worker-after-recovery")
                journal, engine = executor_for(lease)
                observed[op["kind"]] = effect.observation.observed_at
                for oid in op["required_evidence"]:
                    passed, observation = measure(obligations[oid])
                    h["prove"](oid, VerificationResult.PASS if passed else
                               VerificationResult.FAIL,
                               artifact_digest=artifact_digest(observation),
                               obligation=next(o for o in plan.obligations
                                               if o.obligation_id == oid),
                               revisions=plan.revisions)
                record = h["coordinator"].reconcile(
                    revision, plan, outcome=EffectOutcome.EFFECT_CONFIRMED
                    if effect.status == EffectStatus.APPLIED else EffectOutcome.EFFECT_UNKNOWN,
                    actor_id="coordinator")
                events.append((op["kind"], str(record.state), record.reason, None))
                continue
            effect = engine.execute(command, permit_for(command), adapter)
            observed[op["kind"]] = effect.observation.observed_at
        for oid in op["required_evidence"]:
            passed, observation = measure(obligations[oid])
            h["prove"](oid, VerificationResult.PASS if passed else VerificationResult.FAIL,
                       artifact_digest=artifact_digest(observation),
                       obligation=next(o for o in plan.obligations if o.obligation_id == oid),
                       revisions=plan.revisions)
        record = h["coordinator"].advance_from_ledger(revision, plan, effect=effect,
                                                      actor_id="coordinator")
        if record.state == HandoffState.SAFE_HOLD:
            break
    # This executor is done; ownership is released only with no unresolved effect.
    ownership.release(lease)
    return record, observed


def pieces(business, adapter_id, *, source, target, route):
    names, app = business["names"], business["app"]
    adapter = PostgresOfflineCopyAdapter(
        adapter_id, connect=business["connect"], source_db=names[source],
        target_db=names[target], app_role=app, tables=TABLES, schema_sql=SCHEMA, route=route)
    probes = PostgresHandoffProbes(
        connect_as=business["connect_as"], admin_connect=business["connect"],
        source_db=names[source], target_db=names[target], app_role=app, tables=TABLES,
        probe_insert=PROBE_INSERT)
    return adapter, probes


def run_handoff(postgres_database, now, business, *, adjust=None, until=None,
                strategy="offline-copy", before=None, lose_response_at=(), events=None):
    h = admitted_handoff(postgres_database, now, adjust=adjust, strategy=strategy)
    names = business["names"]
    route = Route(names["src"])
    adapter, probes = pieces(business, h["world"]["manifest"].adapter_id,
                             source="src", target="tgt", route=route)
    record, observed = drive(h, h["revision"], h["plan"], adapter=adapter, probes=probes,
                             other_db=names["other"], scratch_db=names["scratch"],
                             operation=OPERATION, approval_id="approval-1", until=until,
                             before=before, lose_response_at=lose_response_at,
                             events=events)
    return dict(h, record=record, adapter=adapter, probes=probes, route=route,
                business=business, observed=observed)


def without_transport_control(data):
    controls = data["spec"]["security"]["required_controls"]
    data["spec"]["security"]["required_controls"] = [
        c for c in controls if c != "verified_transport_encryption"]


def test_data_really_moves_and_authority_really_changes(postgres_database, now, business):
    names, app = business["names"], business["app"]
    lingering = business["connect_as"](names["src"], app)  # an app session left open
    lingering.execute("SELECT 1")
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE
    assert x["record"].owner_environment == "onprem-target"
    # The fence ended existing sessions, not only new logins (§15.5).
    with pytest.raises(psycopg.OperationalError):
        lingering.execute("INSERT INTO orders VALUES (900, 'late', 'late', 1)")
    lingering.close()
    # The data is where the route now points, and business continues there.
    assert x["route"].get() == names["tgt"]
    for table, _ in TABLES:
        assert x["probes"].content_equal(table)[0]
    with business["connect_as"](x["route"].get(), app) as conn:
        conn.execute("INSERT INTO orders VALUES (51, 'customer-1', 'key-51', 510)")
        conn.execute("INSERT INTO idempotency_results VALUES ('customer-1', 'key-51', 'ok')")
    assert x["probes"].order_effects_once()[0]
    assert x["probes"].write_denied("source")[0]  # the source never reopened
    spent = (x["observed"]["activate_target"] - x["observed"]["pause_admission"])
    assert spent.total_seconds() * 1000 <= 5000  # measured, within the contract budget


def test_unencrypted_target_is_refused_before_activation(postgres_database, now, business):
    """The default contract requires transport encryption; the local server has
    ssl off. The probe fails at the activation-intent gate, the handoff holds
    as a contract breach before activation, and the source can be restored."""
    names, app = business["names"], business["app"]
    x = run_handoff(postgres_database, now, business)
    held = x["record"]
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "CONTRACT_BREACH"
    assert held.activation_possible is False and x["route"].get() == names["src"]
    assert x["probes"].transport_encrypted("target") == (False, {
        "probe": "transport_encrypted", "ssl": "off"})
    # Abort: the target never took writes; restore the source and prove it.
    assert not x["probes"].single_writer()[0]
    x["coordinator"].abort(x["revision"], x["plan"], target_inactive_verified=True,
                           actor_id="coordinator")
    with business["connect"](names["src"]) as conn:
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(names["src"]), sql.Identifier(app)))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                             "public TO {}").format(sql.Identifier(app)))
    # Restoring starts first; it ends only once a probe shows the source writable.
    x["coordinator"].source_restored(x["revision"], x["plan"], source_writes_verified=False,
                                     actor_id="coordinator")
    restored_writes = not x["probes"].write_denied("source")[0]
    assert restored_writes
    final = x["coordinator"].source_restored(
        x["revision"], x["plan"], source_writes_verified=restored_writes,
        actor_id="coordinator")
    assert final.state == HandoffState.ABORTED


def test_failback_brings_back_writes_made_on_the_target(postgres_database, now, business):
    """AT-30: after the target took writes, a separately approved failback returns
    the authority, and the target's new data, to the original source."""
    names, app = business["names"], business["app"]
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control,
                    until="stabilize")
    assert x["record"].state == HandoffState.ROUTE_CONFIRMED
    with business["connect_as"](x["route"].get(), app) as conn:  # business on the target
        for i in range(51, 56):
            conn.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                         (i, "customer-9", f"key-{i}", i))
            conn.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                         ("customer-9", f"key-{i}"))
    forward = x["coordinator"].require_failback(
        x["revision"], x["plan"], reason="post-activation latency breach",
        actor_id="coordinator")
    assert forward.state == HandoffState.FAILBACK_REQUIRED

    world = build_world(datetime.now(UTC), contract=x["world"]["contract"],
                        strategy=FAILBACK_STRATEGY, reverse=True,
                        environment="synthetic-target", uid_prefix="target-uid-")
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    unit = json.loads(x["revision"].canonical)["unit"]["unit_digest"]

    def recompile():
        return compile_failback(
            x["revision"], x["store"].current(x["app"], unit), world["contract"],
            world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
            expected_tenant=x["tenant"], source=world["source"], target=world["target"],
            manifest=world["manifest"], qualification=world["qualification"], rows=2000,
            authenticate=world["auth"], clock=lambda: datetime.now(UTC))

    revision, result = recompile()
    assert result["status"] == "COMPILED", result["blockers"]
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    verifiers = {k: x["verifier"] for k in kinds}
    plan = lower_plan(revision, created_by="planner", verifiers=verifiers)
    x["lifecycle"].save_plan(plan)
    x["lifecycle"].save_approval(x["app"], plan.plan_id, "approval-failback", PlanApproval(
        plan.fingerprint, "security-approver", PlanPurpose.MIGRATION_PHASE,
        datetime.now(UTC) + timedelta(minutes=3)))
    x["trust_scopes"]({o.scope for o in plan.obligations})
    complete_feeds(x["feed"], revision, plan)
    HandoffAdmission(lifecycle=x["lifecycle"], claims=x["claims"], changes=x["changes"],
                     compiled=x["compiled"], clock=lambda: datetime.now(UTC)).admit(
        x["app"], "approval-failback", recompile=recompile, verifiers=verifiers)

    back_adapter, back_probes = pieces(business, world["manifest"].adapter_id,
                                       source="tgt", target="src", route=x["route"])
    record, _ = drive(x, revision, plan, adapter=back_adapter, probes=back_probes,
                      other_db=names["other"], scratch_db=names["scratch"],
                      operation="real-failback", approval_id="approval-failback")
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert record.owner_environment == "aws-source" and record.predecessor == (
        x["revision"].digest)
    assert x["route"].get() == names["src"]
    with business["connect"](names["src"]) as conn:  # the target's writes came back
        returned = conn.execute(
            "SELECT count(*) FROM orders WHERE customer='customer-9'").fetchone()[0]
    assert returned == 5
    assert back_probes.single_writer()[0]  # the original source writes, the target not
    assert back_probes.order_effects_once()[0]


def test_lost_activation_response_is_held_then_reconciled_from_the_databases(
        postgres_database, now, business):
    """§29.2-8, AT-20: the target was really activated but the executor lost the
    response. The handoff holds (activation may have happened), nothing is
    re-run, and a read-only look at the databases confirms the effect."""
    events = []
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control,
                    lose_response_at=("activate_target",), events=events)
    lost, resumed = events
    assert lost == ("activate_target", "SAFE_HOLD", "EFFECT_UNKNOWN", True)
    assert resumed[:2] == ("activate_target", "TARGET_ACTIVE_OBSERVED")
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE
    assert x["probes"].single_writer()[0]


def test_snapshot_cdc_keeps_writes_made_after_the_initial_sync(postgres_database, now,
                                                              business):
    names, app = business["names"], business["app"]

    def write_during_sync(step):
        if step == "pause_admission":  # after the initial copy, before the fence
            with business["connect_as"](names["src"], app) as conn:
                conn.execute("INSERT INTO orders VALUES (77, 'late', 'key-77', 7)")
                conn.execute("INSERT INTO idempotency_results VALUES "
                             "('late', 'key-77', 'confirmed')")

    x = run_handoff(postgres_database, now, business, adjust=without_transport_control,
                    strategy="snapshot-cdc", before=write_during_sync)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE
    with business["connect"](names["tgt"]) as conn:
        assert conn.execute("SELECT count(*) FROM orders WHERE id=77").fetchone()[0] == 1
    assert x["probes"].content_equal("orders")[0]
