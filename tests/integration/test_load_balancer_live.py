"""Traffic switched at the load balancer, measured in both clouds.

The route store of a data adapter's ``commit_route`` is a real load balancer:
an AWS Application Load Balancer's listener, and a Google Cloud external
Application Load Balancer's URL map. Each side is a small serverless backend
(Lambda behind a target group; Cloud Run behind a serverless NEG) answering
with a run-unique token, and every observation is a real request through the
load balancer from this machine, the only address either admits.
"""

from datetime import UTC, datetime, timedelta
from functools import partial

from aws_fixtures import http_marker
import pytest

from sova.adapters.aws_alb_route import AlbListenerRoute
from sova.adapters.gcp_url_map_route import UrlMapRoute
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

SIDES = ("orders_src", "orders_tgt")


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "edge", "unit-route"), "op-route", "route",
        adapter.adapter_id, "commit_route", "op-route:route", "c" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=30), "{}")


def adapter_with(route):
    return PostgresOfflineCopyAdapter(
        "postgres-offline-copy", connect=lambda database: None, source_db=SIDES[0],
        target_db=SIDES[1], app_role="orders_app", tables=(("orders", "id"),), schema_sql="",
        route=route)


def observer(pair):
    tokens = {SIDES[0]: pair["tokens"]["src"], SIDES[1]: pair["tokens"]["tgt"]}
    return partial(http_marker, pair["url"], tokens)


def switch_and_check(route):
    assert route.get() == SIDES[0]  # the source serves everything
    adapter = adapter_with(route)
    cmd = command(adapter)
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.UNKNOWN
    adapter.validate(cmd)
    adapter.apply(cmd)
    observed = route.observe()
    assert observed["route"] == SIDES[1], observed  # configured and served alike
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    adapter.apply(cmd)  # idempotent
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED


@pytest.mark.aws_live
def test_an_alb_listener_switches_real_traffic_to_the_target(alb_pair):
    route = AlbListenerRoute(alb_pair["elb"], listener_arn=alb_pair["listener"],
                             sides={SIDES[0]: alb_pair["groups"]["src"],
                                    SIDES[1]: alb_pair["groups"]["tgt"]},
                             observe=observer(alb_pair))
    switch_and_check(route)
    # Someone moves the listener back outside the plan: the route is not the target.
    alb_pair["elb"].modify_listener(ListenerArn=alb_pair["listener"], DefaultActions=[
        {"Type": "forward", "TargetGroupArn": alb_pair["groups"]["src"]}])
    assert adapter_with(route).readback(command(adapter_with(route)), datetime.now(UTC)).status \
        == EffectStatus.UNKNOWN
    # A listener doing something else (a fixed answer) is never overwritten.
    alb_pair["elb"].modify_listener(ListenerArn=alb_pair["listener"], DefaultActions=[
        {"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "503"}}])
    with pytest.raises(ContractError, match="something else"):
        route.set(SIDES[1])


@pytest.mark.gcp_live
def test_a_url_map_switches_real_traffic_to_the_target(gcp_lb_pair):
    pair = gcp_lb_pair
    route = UrlMapRoute(pair["rest"], url_map=pair["url_map"],
                        sides={SIDES[0]: pair["backends"]["src"],
                               SIDES[1]: pair["backends"]["tgt"]},
                        observe=observer(pair))
    started = datetime.now(UTC)
    switch_and_check(route)
    print(f"URL map switch served everywhere after "
          f"{(datetime.now(UTC) - started).total_seconds():.0f}s")
    # A write prepared from an earlier read is refused: the fingerprint moved.
    stale = route._map()
    pair["rest"].patch(route.url, {"defaultService": stale["defaultService"].replace(
        pair["backends"]["tgt"], pair["backends"]["src"]), "fingerprint": stale["fingerprint"]})
    route._map = lambda: stale
    with pytest.raises(ContractError, match="changed in between"):
        route.set(SIDES[0])
