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
from handoff_drive import (
    drive,
    pieces,
    run_handoff,
    without_transport_control,
)
from handoff_support import complete_feeds
from postgres_business import TABLES
import psycopg
from psycopg import sql
import pytest

from sova.adapters.postgres_handoff_probes import artifact_digest
from sova.application.handoff_admission import HandoffAdmission
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState
from sova.domain.plan_compiler import FAILBACK_STRATEGY, compile_failback
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose

pytestmark = pytest.mark.postgres
def test_data_really_moves_and_authority_really_changes(postgres_database, now, business):
    names, app = business["names"], business["app"]
    lingering = business["connect_as"](names["src"], app)  # an app session left open
    lingering.execute("SELECT 1")
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
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
    assert x["record"].state == HandoffState.ROUTE_CONFIRMED, (x["record"].reason,
                                                         x["record"].held_from)
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
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
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
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
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
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
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
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
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
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
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
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
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


def test_sequences_move_with_the_data_so_numbers_are_never_reissued(postgres_database,
                                                                   now, business):
    """AT-24: rows alone are not the state. The source issued order numbers 1-10;
    after the handoff the target continues at 11 instead of issuing 1 again."""
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                         x["record"].held_from)
    with business["connect_as"](x["route"].get(), business["app"]) as conn:
        assert conn.execute("SELECT nextval('order_number_seq')").fetchone()[0] == 11


@pytest.mark.parametrize("difference", [
    "ALTER TABLE orders ADD COLUMN channel text",
    "ALTER TABLE orders ADD CONSTRAINT positive_amount CHECK (amount >= 0)",
])
def test_a_schema_difference_stops_staging_before_any_source_change(postgres_database, now,
                                                                    business, difference):
    """AT-24: the source has a column the target definition lacks. Staging is not
    confirmed, the handoff holds at its first step, and the source was never
    paused or fenced."""
    names = business["names"]
    with business["connect"](names["src"]) as conn:
        conn.execute(difference)
    x = run_handoff(postgres_database, now, business, adjust=without_transport_control)
    assert x["record"].state == HandoffState.SAFE_HOLD
    assert x["record"].reason == "EFFECT_UNKNOWN" and x["record"].next_operation == 0
    assert not x["probes"].write_denied("source")[0]  # business still runs on the source
