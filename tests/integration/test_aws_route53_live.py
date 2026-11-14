"""DNS-level traffic switch on Amazon Route 53, measured at the authoritative servers.

A run-scoped public hosted zone (never delegated, deleted within the hour, so
it costs nothing) holds the name clients use. The PostgreSQL adapter's route
step switches it from the source to the target address; an independent probe
asks the zone's own name servers with ``dig``. The addresses are from the
documentation ranges: this tests the route's control, not reachability.
"""

from datetime import UTC, datetime, timedelta
import shutil
import subprocess
from uuid import uuid4

import pytest

from sova.adapters.aws_route53_route import Route53Route
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

pytestmark = pytest.mark.aws_live
SIDES = {"orders_src": "198.51.100.10", "orders_tgt": "203.0.113.20"}


@pytest.fixture
def zone(aws):
    if not shutil.which("dig"):
        pytest.skip("dig is required to ask the authoritative servers")
    r53 = aws["session"].client("route53")
    name = f"sova-test-{aws['run']}-{uuid4().hex[:6]}.com"
    created = r53.create_hosted_zone(Name=name, CallerReference=uuid4().hex,
                                     HostedZoneConfig={"Comment": "sova test, run-scoped"})
    zone_id = created["HostedZone"]["Id"].rsplit("/", 1)[-1]
    r53.change_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id,
                                 AddTags=aws["tags"])
    try:
        yield {"r53": r53, "id": zone_id, "name": name,
               "servers": created["DelegationSet"]["NameServers"]}
    finally:
        records = r53.list_resource_record_sets(HostedZoneId=zone_id)["ResourceRecordSets"]
        changes = [{"Action": "DELETE", "ResourceRecordSet": r} for r in records
                   if r["Type"] not in ("NS", "SOA")]
        if changes:
            r53.change_resource_record_sets(HostedZoneId=zone_id,
                                            ChangeBatch={"Changes": changes})
        r53.delete_hosted_zone(Id=zone_id)


def authoritative(zone, record):
    """What the zone's own name servers answer, asked over DNS."""
    answers = set()
    for server in zone["servers"]:
        out = subprocess.run(["dig", "+short", "+time=3", "+tries=2", f"@{server}", record, "A"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        answers.add(tuple(sorted(out)))
    return answers


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "edge", "unit-route"), "op-route", "route",
        adapter.adapter_id, "commit_route", "op-route:route", "c" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), "{}")


def test_commit_route_switches_the_dns_name_at_the_authoritative_servers(zone):
    record = "orders." + zone["name"]
    route = Route53Route(zone["r53"], zone_id=zone["id"], name=record, sides=SIDES, ttl=60)
    route.set("orders_src")
    assert route.get() == "orders_src"
    assert authoritative(zone, record) == {(SIDES["orders_src"],)}
    adapter = PostgresOfflineCopyAdapter(
        "postgres-offline-copy", connect=lambda database: None, source_db="orders_src",
        target_db="orders_tgt", app_role="orders_app", tables=(("orders", "id"),),
        schema_sql="", route=route)
    cmd = command(adapter)
    adapter.validate(cmd)
    adapter.apply(cmd)
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    assert authoritative(zone, record) == {(SIDES["orders_tgt"],)}  # every server agrees
    observed = route.observe()
    assert observed["ttl"] == 60 and observed["change_in_sync"]
    adapter.apply(cmd)  # idempotent: nothing to change
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED


def test_a_change_made_in_between_is_refused_and_an_outside_change_reads_unknown(zone):
    record = "orders." + zone["name"]
    route = Route53Route(zone["r53"], zone_id=zone["id"], name=record, sides=SIDES, ttl=60)
    route.set("orders_src")
    stale = route._record()
    # Someone points the name elsewhere outside the plan.
    zone["r53"].change_resource_record_sets(HostedZoneId=zone["id"], ChangeBatch={"Changes": [{
        "Action": "UPSERT", "ResourceRecordSet": {
            "Name": record + ".", "Type": "A", "TTL": 60,
            "ResourceRecords": [{"Value": "192.0.2.99"}]}}]})
    assert route.get() is None  # neither side: the name serves something else
    route._record = lambda: stale  # a switch prepared from the earlier read
    with pytest.raises(ContractError, match="changed in between"):
        route.set("orders_tgt")
    adapter = PostgresOfflineCopyAdapter(
        "postgres-offline-copy", connect=lambda database: None, source_db="orders_src",
        target_db="orders_tgt", app_role="orders_app", tables=(("orders", "id"),),
        schema_sql="", route=Route53Route(zone["r53"], zone_id=zone["id"], name=record,
                                          sides=SIDES))
    assert adapter.readback(command(adapter), datetime.now(UTC)).status == \
        EffectStatus.UNKNOWN
