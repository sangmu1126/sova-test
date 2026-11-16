"""A MongoDB database (orders and their recorded results) handed off by the
whole control plane, and the database probes failing where they must."""

import json

from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
from mongodb_business import as_user
from mongodb_world import COLLECTIONS, MongoWorld, Route
import pytest

from sova.adapters.mongodb_probes import MongoDatabaseProbes
from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.mongodb_execution import mongodb_factory

pytestmark = [pytest.mark.postgres, pytest.mark.mongodb, pytest.mark.kubernetes]
UNIQUE = {"ORDER-01": {"collection": "orders", "unique": ["customer", "idempotency_key"]}}


def settings_of(b):
    def connect_as(side, writer):
        return as_user(b, side, writer.rsplit("@", 1)[0])

    return {"connect": lambda side: b["admin"][side], "connect_as": connect_as,
            "databases": {"source": b["names"]["src"], "target": b["names"]["tgt"]},
            "collections": COLLECTIONS, "writers": b["writers"],
            "app_user": f"{b['users']['app']}@admin", "invariants": UNIQUE,
            "other_db": b["names"]["other"], "zone_of": lambda side: "approved-zone-a"}


def test_a_database_is_handed_off_by_the_control_plane(postgres_database, now, mongo_business,
                                                       tmp_path):
    b = mongo_business
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + b["names"]["src"][-8:] * 4
    data["spec"]["data"]["protected_sets"] = list(COLLECTIONS)
    without_transport_control(data)  # the disposable servers have no TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="mongodb")
    route = Route(b["names"]["src"])
    settings = {**settings_of(b), "admin_user": "sova-admin@admin", "route": route,
                "scratch_db": f"scratch_{b['names']['src'][-8:]}"}
    worker = worker_for(h, mongodb_factory(h["adapter_id"], lambda t, a: settings),
                        owner="worker-mongodb")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert route.get() == b["names"]["tgt"]
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    assert seen["business.ORDER-01"]["documents"] == 40
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith("availability.")}))


def test_each_database_probe_fails_on_what_it_measures(mongo_business):
    from adapter_conformance import apply

    b = mongo_business
    world = MongoWorld(b)
    p = MongoDatabaseProbes(**settings_of(b))
    passed, live = p.write_denied("source")
    assert not passed and {o["attempt"] for o in live["outcomes"].values()} == {
        "write_accepted"}
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "activate_target"):
        apply(world.adapter(), action)
    for check in (lambda: p.write_denied("source"), p.single_writer,
                  lambda: p.content_equal("orders"), lambda: p.invariant("ORDER-01"),
                  p.cross_tenant_denied,
                  lambda: p.restore_drill(f"scratch_{b['names']['src'][-8:]}")):
        passed, observation = check()
        assert passed, observation
    world.tamper_target()
    assert not p.content_equal("orders")[0]
    target = b["admin"]["target"][b["names"]["tgt"]].orders
    target.drop_index("one_order_per_key")
    target.insert_one({"_id": 4242, "customer": "customer-1", "idempotency_key": "key-1"})
    passed, invariant = p.invariant("ORDER-01")
    assert not passed and invariant["violations"]
    b["admin"]["target"].admin.command("grantRolesToUser", b["users"]["app"], roles=[
        {"role": "read", "db": b["names"]["other"]}])
    assert not p.cross_tenant_denied()[0]
    assert not p.transport_encrypted("target")[0]
