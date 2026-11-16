"""MySQL change capture through the whole control plane, on the snapshot-cdc
template: the bulk reaches the target before the write stop (``start_sync``),
the stop carries only what changed (``apply_final_changes``), and the restore
drill, which needs the data on the target, is measured before the stop."""

import json
from uuid import uuid4

from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
from mysql_business import PROBE_INSERT, SCHEMA, TABLES
from mysql_pair_business import REPLICATION
from mysql_world import Route
import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.mysql_execution import mysql_factory

pytestmark = [pytest.mark.postgres, pytest.mark.mysql, pytest.mark.kubernetes]


@pytest.fixture
def replication_handoff(postgres_database, now, mysql_pair_business, tmp_path):
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + uuid4().hex
    without_transport_control(data)  # the disposable servers have no TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="mysql",
                 strategy="snapshot-cdc")
    b, names = mysql_pair_business, mysql_pair_business["names"]
    route = Route(names["src"])
    settings = {
        "connect": b["connect"], "connect_as": b["connect_as"], "source_db": names["src"],
        "target_db": names["tgt"], "scratch_db": names["scratch"], "other_db": names["other"],
        "app_role": b["app"], "writer_roles": (b["app"], b["batch"]), "tables": TABLES,
        "schema_sql": SCHEMA, "probe_insert": PROBE_INSERT, "route": route,
        "zone_of": lambda database: "approved-zone-a",
        "replication": {"source_endpoint": b["source_endpoint"],
                        "account": f"{REPLICATION[0]}@%", "password": REPLICATION[1]}}
    h["worker"] = worker_for(h, mysql_factory(h["adapter_id"], lambda t, a: settings,
                                              replication=True), owner="worker-mysql-cdc")
    return {**h, "business": b, "route": route}


def test_a_mysql_unit_is_handed_off_by_change_capture(replication_handoff):
    h, b = replication_handoff, replication_handoff["business"]
    names = b["names"]
    assert h["adapter_id"] == "mysql-snapshot-cdc"
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == names["tgt"]
    with b["connect_as"](names["tgt"], b["app"]) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO orders VALUES (5001, 'after', 'after-1', 5)")
        conn.commit()
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    order = list(seen)  # in the order they were first measured
    # The data is on the target before the stop, so its restore drill is too.
    assert order.index("security.tested_restore") < order.index("fence.source_writes_denied")
    assert seen["security.tested_restore"]["tables"] == {t: True for t, _ in TABLES}
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith("availability.")}))
