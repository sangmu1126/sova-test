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
from sova.ports.execution import RecoveryRequired

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
    batch = "orders_batch_" + suffix  # a second writer path: a nightly batch job
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(app)))
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(batch)))
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
        conn.execute(sql.SQL("GRANT INSERT ON orders TO {}").format(sql.Identifier(batch)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(names["src"]), sql.Identifier(batch)))  # its own, not PUBLIC's
    with connect_as(names["src"], app) as conn:  # business before the handoff
        for i in range(1, 51):
            conn.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                         (i, f"customer-{i % 7}", f"key-{i}", i * 10))
            conn.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                         (f"customer-{i % 7}", f"key-{i}"))
            conn.execute("INSERT INTO work_items VALUES (%s, %s, 'done')", (i, i))
    yield dict(names=names, app=app, batch=batch, connect=connect, connect_as=connect_as,
               dsn=dsn)


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
          approval_id, until=None, before=None, lose_response_at=(), events=None,
          after_effect=None):
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
            try:
                effect = engine.execute(command, permit_for(command), adapter)
            except UncertainEffect:
                # The readback did not confirm the postcondition: hold, do not guess.
                record = h["coordinator"].advance_from_ledger(
                    revision, plan, effect=journal.get(command), actor_id="coordinator")
                break
            observed[op["kind"]] = effect.observation.observed_at
            if after_effect:
                after_effect(op)
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
    try:
        ownership.release(lease)
    except RecoveryRequired:
        pass  # an unknown effect keeps the app in recovery, as the ledger requires
    return record, observed


def pieces(business, adapter_id, *, source, target, route, fenced=None):
    """The adapter fences ``fenced`` writer roles (default: every registered one);
    the probes always check every writer path that really exists."""
    names, app = business["names"], business["app"]
    writers = (app, business["batch"])
    adapter = PostgresOfflineCopyAdapter(
        adapter_id, connect=business["connect"], source_db=names[source],
        target_db=names[target], app_role=app, tables=TABLES, schema_sql=SCHEMA, route=route,
        writer_roles=fenced or writers)
    probes = PostgresHandoffProbes(
        connect_as=business["connect_as"], admin_connect=business["connect"],
        source_db=names[source], target_db=names[target], app_role=app, tables=TABLES,
        probe_insert=PROBE_INSERT, writer_roles=writers)
    return adapter, probes


def run_handoff(postgres_database, now, business, *, adjust=None, until=None,
                strategy="offline-copy", before=None, lose_response_at=(), events=None,
                fenced=None, watch_catalog=None):
    h = admitted_handoff(postgres_database, now, adjust=adjust, strategy=strategy)
    after_effect = None
    if watch_catalog:
        before, after_effect = watch_catalog(h, before)
    names = business["names"]
    route = Route(names["src"])
    adapter, probes = pieces(business, h["world"]["manifest"].adapter_id,
                             source="src", target="tgt", route=route, fenced=fenced)
    record, observed = drive(h, h["revision"], h["plan"], adapter=adapter, probes=probes,
                             other_db=names["other"], scratch_db=names["scratch"],
                             operation=OPERATION, approval_id="approval-1", until=until,
                             before=before, lose_response_at=lose_response_at,
                             events=events, after_effect=after_effect)
    return dict(h, record=record, adapter=adapter, probes=probes, route=route,
                business=business, observed=observed)


def without_transport_control(data):
    """The local reference profile: no TLS on the disposable server, and budgets
    wide enough that a loaded test machine does not decide the outcome."""
    controls = data["spec"]["security"]["required_controls"]
    data["spec"]["security"]["required_controls"] = [
        c for c in controls if c != "verified_transport_encryption"]
    data["spec"]["availability"]["max_write_admission_pause_ms"] = 60000
    data["spec"]["availability"]["max_client_completion_wait_ms"] = 120000


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
    assert 0 < spent.total_seconds() * 1000 <= 60000  # measured against the budget


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


def test_a_restored_source_writer_is_caught_and_holds_the_service(postgres_database, now,
                                                                  business):
    """AT-32 and §15.6: after completion someone restores the source database
    from a snapshot whose grants let the old writer back in. A fresh
    write-denial probe fails, the watch opens a security incident, and the
    service is held: no new admission or command."""
    from sova.adapters.postgres_governance import PostgresSecurityHolds
    from sova.application.source_fence_watch import SourceFenceWatch

    names, app = business["names"], business["app"]
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE
    holds = PostgresSecurityHolds(x["connect"], tenant_id=x["tenant"])
    watch = SourceFenceWatch(store=x["store"], evidence=x["coordinator"].evidence,
                             holds=holds, clock=lambda: datetime.now(UTC))

    def observe():
        passed, observation = x["probes"].write_denied("source")
        x["prove"]("fence.source_writes_denied",
                   VerificationResult.PASS if passed else VerificationResult.FAIL,
                   artifact_digest=artifact_digest(observation))
        return watch.check(x["revision"], x["plan"], actor_id="fence-watch")

    assert observe()["status"] == "FENCED"
    with business["connect"](names["src"]) as conn:  # what the old snapshot restores
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(names["src"]), sql.Identifier(app)))
        conn.execute(sql.SQL("GRANT INSERT ON ALL TABLES IN SCHEMA public TO {}").format(
            sql.Identifier(app)))
    caught = observe()
    assert caught["status"] == "SOURCE_WRITER_REACTIVATED" and caught["newly_opened"]
    assert observe()["hold_id"] == caught["hold_id"]  # one incident, not one per check
    assert x["store"].active_hold(x["app"])


def test_a_measured_pause_over_budget_requires_failback(postgres_database, now, business):
    """The write-pause budget is measured from ledger observations, not assumed:
    with a 1 ms budget the real pause always exceeds it, and since the budget
    is verified after activation the handoff requires failback."""
    def tight(data):
        without_transport_control(data)
        data["spec"]["availability"]["max_write_admission_pause_ms"] = 1

    x = run_handoff(postgres_database, now, business, adjust=tight)
    assert x["record"].state == HandoffState.FAILBACK_REQUIRED
    assert x["record"].reason == "CONTRACT_BREACH" and x["record"].activation_possible


def test_independence_is_measured_with_the_source_really_unreachable(
        postgres_database, now, business):
    """AT-35: with the source database blocked, a new application session still
    does business on the target. What cannot be measured here (source compute,
    platform revocation) stays UNKNOWN, so provider exit is not claimed."""
    names = business["names"]
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    with psycopg.connect(business["dsn"], autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
            sql.Identifier(names["src"])))
    measured = {
        "exit.removed.source-db.blocked_cold_start": x["probes"].cold_start_without_source,
        "exit.restart.cold_start_fresh_credentials": x["probes"].cold_start_without_source,
        "exit.operations.batch_backup_restore_audit_deploy":
            lambda: x["probes"].restore_drill(names["scratch"]),
    }
    for o in x["readiness"].obligations(x["revision"], x["plan"]):
        if o.obligation_id in measured:
            passed, observation = measured[o.obligation_id]()
            x["prove"](o.obligation_id, VerificationResult.PASS if passed else
                       VerificationResult.FAIL, obligation=o,
                       artifact_digest=artifact_digest(observation))
    levels = {k: v["status"] for k, v in
              x["readiness"].assess(x["revision"], x["plan"])["levels"].items()}
    assert levels["DATA_IDENTITY_INDEPENDENT"] == "PASS"
    assert levels["RESTART_VERIFIED"] == "PASS" and levels["OPERATIONS_VERIFIED"] == "PASS"
    assert levels["COMPUTE_INDEPENDENT"] == "UNKNOWN"
    assert levels["PLATFORM_EXIT_VERIFIED"] == "UNKNOWN"
    assert levels["PROVIDER_EXIT_VERIFIED"] == "UNKNOWN"


def test_a_writer_missing_from_the_inventory_is_caught_by_the_probe(postgres_database, now,
                                                                     business):
    """The fence covers every registered writer path (§15.5). If the adapter is
    told only about the application, the independent probe still finds the
    batch job writing and the handoff holds before activation."""
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control,
                    fenced=(business["app"],))
    assert x["record"].state == HandoffState.SAFE_HOLD
    assert x["record"].reason == "CONTRACT_BREACH" and not x["record"].activation_possible
    denied, observation = x["probes"].write_denied("source")
    assert not denied and observation["outcomes"][business["batch"]] == "write_accepted"


def test_a_prepared_transaction_keeps_the_fence_unconfirmed(postgres_database, now,
                                                            business):
    """A prepared transaction outlives its session and could still commit, so the
    fence readback stays UNKNOWN and the handoff holds."""
    names, app = business["names"], business["app"]
    with business["connect"](names["src"]) as conn:
        if conn.execute("SHOW max_prepared_transactions").fetchone()[0] == "0":
            pytest.skip("cluster started without prepared transactions")
    gid = "in-flight-" + uuid4().hex[:8]

    def prepare(step):
        if step == "pause_admission":
            conn = business["connect_as"](names["src"], app)
            conn.execute("INSERT INTO orders VALUES (88, 'prepared', 'key-88', 8)")
            conn.execute(sql.SQL("PREPARE TRANSACTION {}").format(sql.Literal(gid)))
            conn.close()

    try:
        x = run_handoff(postgres_database, now, business, adjust=without_transport_control,
                        before=prepare)
        assert x["record"].state == HandoffState.SAFE_HOLD
        assert x["record"].reason == "EFFECT_UNKNOWN"
        assert not x["record"].activation_possible
    finally:
        with psycopg.connect(business["dsn"], dbname=names["src"], autocommit=True) as admin:
            admin.execute(sql.SQL("ROLLBACK PREPARED {}").format(sql.Literal(gid)))


def catalog_watch(business, *, foreign=None):
    """Poll the source and target catalogs: unattributed before each step (anyone's
    change), attributed right after each of the plan's own effects."""
    from sova.adapters.postgres_change_collector import PostgresCatalogCollector

    names = business["names"]

    def install(h, before):
        collector = PostgresCatalogCollector(
            h["feed"], connect=business["connect"], tenant_id=h["tenant"],
            application_id=h["app"], collector_id="change-collector-1",
            databases={names["src"]: ("synthetic-source", "synthetic-uid-database"),
                       names["tgt"]: ("onprem-target", None)})
        collector.poll()  # baseline

        def poll_before(step):
            if foreign:
                foreign(step)
            collector.poll()
            if before:
                before(step)

        def poll_after(op):
            collector.poll(cause=(h["plan"].fingerprint, op["operation_id"]))

        return poll_before, poll_after

    return install


def test_catalog_changes_by_the_plan_itself_do_not_stop_it(postgres_database, now,
                                                          business):
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control,
                    watch_catalog=catalog_watch(business))
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE
    recorded = x["feed"].events(application_id=x["app"],
                                environments=("synthetic-source", "onprem-target"),
                                since=now)
    assert {str(e.kind) for e in recorded} == {"policy"}  # pause, fence, stage, activate
    assert all(e.cause is not None for e in recorded)


def test_a_foreign_schema_change_mid_handoff_stops_the_next_command(postgres_database, now,
                                                                   business):
    """INV-08 with a real collector: someone alters a source table after the
    snapshot; the next command is refused until the plan is re-evaluated."""
    from sova.domain.change_impact import PlanReevaluationRequired

    names = business["names"]

    def alter(step):
        if step == "fence_source_writes":
            with business["connect"](names["src"]) as conn:
                conn.execute("ALTER TABLE orders ADD COLUMN note text")

    with pytest.raises(PlanReevaluationRequired, match="UNIT_RESOURCE_CHANGED:database"):
        run_handoff(postgres_database, now, business, adjust=without_transport_control,
                    watch_catalog=catalog_watch(business, foreign=alter))


def test_business_continues_on_the_confirmed_route_while_the_platform_is_down(
        postgres_database, now, business):
    """INV-07, AT-34: once the route is confirmed, business does not need the
    control plane. With the platform's database role unable to log in, the
    application still writes on the target through the confirmed route."""
    dsn, runtime_role = postgres_database
    app = business["app"]
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(runtime_role)))
    try:
        with pytest.raises(psycopg.OperationalError):
            x["store"].current(x["app"], x["record"].unit_digest)  # the platform is down
        with business["connect_as"](x["route"].get(), app) as conn:
            conn.execute("INSERT INTO orders VALUES (60, 'customer-2', 'key-60', 60)")
            conn.execute("INSERT INTO idempotency_results VALUES "
                         "('customer-2', 'key-60', 'confirmed')")
        assert x["probes"].order_effects_once()[0]
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("ALTER ROLE {} LOGIN").format(
                sql.Identifier(runtime_role)))
