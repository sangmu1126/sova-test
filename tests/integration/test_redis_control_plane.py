"""A Redis-protocol keyspace (sessions and carts of the board) handed off by the
whole control plane: registered, compiled, approved and admitted, then run by
a worker - the keyspace adapter through the effect ledger, every obligation
measured by the keyspace probes and signed."""

import json

from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
import pytest
from redis_business import as_user
from redis_world import Route

from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.redis_execution import redis_factory

pytestmark = [pytest.mark.postgres, pytest.mark.redis, pytest.mark.kubernetes]
CART = {"id": "CART-01", "description": "모든 세션의 장바구니가 존재한다."}


@pytest.fixture
def keyspace_handoff(postgres_database, now, redis_business, tmp_path):
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + redis_business["prefix"].strip(":")[-8:] * 4
    data["spec"]["data"]["protected_sets"] = ["sessions", "carts"]
    data["spec"]["business"]["invariants"] = [CART]
    without_transport_control(data)  # the disposable servers have no TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="redis")
    b, prefix = redis_business, redis_business["prefix"]
    route = Route("source")
    settings = {
        "connect": lambda side: b["admin"][side],
        "connect_as": lambda side, name: as_user(b, side, name),
        "prefixes": (prefix + "*",), "writers": (b["users"]["app"], b["users"]["batch"]),
        "app_user": b["users"]["app"], "admin_user": "sova-admin", "route": route,
        "routes": ("source", "target"),
        "sets": {"sessions": prefix + "session:", "carts": prefix + "cart:"},
        "invariants": {"CART-01": {"from": prefix + "session:", "field": "cart",
                                   "to": prefix + "cart:"}},
        "scratch_db": 15, "other_prefix": "shop-other:",
        "zone_of": lambda side: "approved-zone-a"}  # the operator's inventory
    h["worker"] = worker_for(h, redis_factory(h["adapter_id"], lambda t, a: settings),
                             owner="worker-redis")
    return {**h, "business": b, "route": route}


def test_a_keyspace_is_handed_off_by_the_control_plane(keyspace_handoff):
    h, b = keyspace_handoff, keyspace_handoff["business"]
    assert h["adapter_id"] == "redis-offline-copy"
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == "target"
    target = as_user(b, "target", b["users"]["app"])
    assert target.hget(b["prefix"] + "session:3", "cart") == b"3"
    target.close()
    with pytest.raises(b["redis"].exceptions.AuthenticationError):
        as_user(b, "source", b["users"]["app"]).ping()
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    assert seen["business.CART-01"]["checked"] == 20 and not seen["business.CART-01"]["missing"]
    assert seen["security.deny_cross_tenant_data_access"]["allowed"] == {"GET": False,
                                                                         "SET": False}
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith(("availability.", "operations."))}))
