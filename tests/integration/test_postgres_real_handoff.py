"""A compiled handoff that really moves data between two local PostgreSQL databases.

Everything runs against the disposable test cluster: a source and a target
database, an application role that writes orders, and independent probes that
measure each obligation. The control plane is unchanged: approval, admission,
authority record, next-step commands through the existing effect ledger, and
signed evidence decide every step. No cloud resource is touched.
"""

from datetime import UTC, datetime
import json
from uuid import uuid4

from handoff_support import admitted_handoff
import psycopg
from psycopg import sql
import pytest
from support import permit_for

from sova.adapters.postgres_effects import PostgresEffectJournal
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes, artifact_digest
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.application.effects import EffectExecutor
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState
from sova.domain.operations import OperationRequest

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
        admin.execute(sql.SQL("COMMENT ON DATABASE {} IS 'approved-zone-a'").format(
            sql.Identifier(names["tgt"])))
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


def run_handoff(postgres_database, now, business, *, adjust=None):
    h = admitted_handoff(postgres_database, now, adjust=adjust)
    names, app = business["names"], business["app"]
    route = Route(names["src"])
    adapter = PostgresOfflineCopyAdapter(
        h["world"]["manifest"].adapter_id, connect=business["connect"],
        source_db=names["src"], target_db=names["tgt"], app_role=app, tables=TABLES,
        schema_sql=SCHEMA, route=route)
    probes = PostgresHandoffProbes(
        connect_as=business["connect_as"], admin_connect=business["connect"],
        source_db=names["src"], target_db=names["tgt"], app_role=app, tables=TABLES,
        probe_insert=PROBE_INSERT)
    lifecycle = h["lifecycle"]
    approval = lifecycle.get_approval(h["app"], "approval-1")
    PostgresOperationRepository(lifecycle).submit(OperationRequest(
        h["tenant"], h["app"], OPERATION, h["plan"].plan_id, h["plan"].fingerprint,
        "approval-1", approval.revision, "operator", "real-handoff-request"))
    ownership = PostgresExecutionOwnership(lifecycle)
    lease = ownership.acquire(h["app"], OPERATION, "worker")
    journal = PostgresEffectJournal(ownership, lease)
    engine = EffectExecutor(journal, authorize=journal.authorize,
                            clock=lambda: datetime.now(UTC))
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    contract = h["world"]["contract"].document["spec"]
    observed = {}  # step kind -> ledger observation time

    def measure(obligation):
        kind = obligation["kind"]
        oid = obligation["id"]
        if kind == "placement_evidence":
            return probes.zone("target", set(contract["residency"][oid.split(".", 1)[1]]))
        if oid == "fence.source_writes_denied" or kind == "stale_route_probe":
            return probes.write_denied("source")
        if kind == "final_content_comparison":
            return probes.content_equal(oid.split(".", 1)[1])
        if kind == "business_invariant":
            return probes.order_effects_once()
        if oid == "security.deny_cross_tenant_data_access":
            return probes.cross_tenant_denied(names["other"])
        if oid == "security.verified_transport_encryption":
            return probes.transport_encrypted("target")
        if kind in ("restore_drill",) or oid == "security.tested_restore":
            return probes.restore_drill(names["scratch"])
        if kind == "authority_readback":
            return probes.single_writer()
        if kind == "measured_budget":
            end = "activate_target" if oid.endswith("write_admission_pause") else "commit_route"
            spent = (observed[end] - observed["pause_admission"]).total_seconds() * 1000
            return spent <= obligation["budget_ms"], {
                "probe": "measured_budget", "spent_ms": round(spent, 3),
                "budget_ms": obligation["budget_ms"]}
        raise AssertionError(f"no probe for {oid}")

    obligations = {o["id"]: o for o in json.loads(h["revision"].canonical)["obligations"]}
    record = None
    for op in h["ops"]:
        effect = None
        if op["adapter"]:
            command = h["coordinator"].next_command(h["revision"], h["plan"],
                                                    operation_id=OPERATION, epoch=lease.epoch)
            effect = engine.execute(command, permit_for(command), adapter)
            observed[op["kind"]] = effect.observation.observed_at
        for oid in op["required_evidence"]:
            passed, observation = measure(obligations[oid])
            h["prove"](oid, VerificationResult.PASS if passed else VerificationResult.FAIL,
                       artifact_digest=artifact_digest(observation))
        record = h["coordinator"].advance_from_ledger(h["revision"], h["plan"], effect=effect,
                                                      actor_id="coordinator")
        if record.state == HandoffState.SAFE_HOLD:
            break
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
