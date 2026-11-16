"""Traffic through a Gateway API entry point (Envoy Gateway on kind): it moves
only when configuration, the Gateway's acceptance and real requests agree, and
someone else's traffic split is never overwritten."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from sova.adapters.kubernetes.gateway_route import GatewayHttpRoute
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

pytestmark = pytest.mark.kubernetes
SIDES = {"web_src": ("web-source", 80), "web_tgt": ("web-target", 80)}


def route_of(b, **options):
    return GatewayHttpRoute(b["controls"](), "web", parents=((b["namespace"], "web"),),
                            sides=SIDES, observe=b["observe"], **options)


def backends(b, value):
    b["run"]("patch", "httproute", "web", "--type=json", "-p", json.dumps(
        [{"op": "replace", "path": "/spec/rules/0/backendRefs", "value": value}]))


def test_traffic_moves_only_when_configured_accepted_and_served(gateway_business):
    route = route_of(gateway_business)
    assert route.get() == "web_src"
    route.set("web_tgt")
    seen = route.observe()
    assert seen["route"] == "web_tgt", seen
    assert seen["answers"] == ["web_tgt"] * 6 and all(seen["accepted"].values())
    route.set("web_tgt")  # idempotent
    assert route.get() == "web_tgt"


def test_a_split_or_a_foreign_backend_is_never_overwritten(gateway_business):
    b = gateway_business
    route = route_of(b)
    backends(b, [{"name": "web-source", "port": 80, "weight": 50},
                 {"name": "web-target", "port": 80, "weight": 50}])
    assert route.get() is None  # two weighted backends: no one side
    with pytest.raises(ContractError, match="not overwritten"):
        route.set("web_tgt")
    assert json.loads(b["run"]("get", "httproute", "web", "-o", "json"))["spec"]["rules"][0][
        "backendRefs"][1]["weight"] == 50  # left as the owner made it


def test_a_route_its_gateway_has_not_accepted_reads_back_as_no_side(gateway_business):
    b = gateway_business
    b["run"]("patch", "httproute", "web", "--type=json", "-p", json.dumps(
        [{"op": "replace", "path": "/spec/parentRefs", "value": [{"name": "nowhere"}]}]))
    seen = route_of(b, samples=2).observe()
    assert seen["configured"] == "web_src" and seen["route"] is None
    assert seen["accepted"] == {f"{b['namespace']}/web": False}


def command(adapter_id, action):
    return EffectCommand(
        ResourceScope("tenant-web", "web", "edge", "unit-web"), "op-gateway", action,
        adapter_id, action, f"op-gateway:{action}", "e" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), "{}")


def test_commit_route_of_a_data_adapter_switches_traffic_at_the_gateway(gateway_business):
    """A data adapter's route step with the HTTPRoute as its route store."""
    b = gateway_business
    adapter = PostgresOfflineCopyAdapter(
        "postgres-offline-copy", connect=lambda database: None, source_db="web_src",
        target_db="web_tgt", app_role="web_app", tables=(("orders", "id"),),
        schema_sql="", route=route_of(b))
    cmd = command(adapter.adapter_id, "commit_route")
    adapter.validate(cmd)
    adapter.apply(cmd)
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    backends(b, [{"name": "web-source", "port": 80}])  # moved back outside the plan
    observation = adapter.readback(cmd, datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
