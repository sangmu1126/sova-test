"""A keyspace on a Valkey cluster: its keys spread over three primaries by
slot, its ACL users kept by each node. The conformance kit holds; the fence
reaches every node the cluster lists and a node it missed keeps it
unconfirmed; the probes find each key's node and restore into scratch keys
of the same slots (a cluster has one database)."""

from datetime import UTC, datetime

from adapter_conformance import HandoffAdapterConformance, apply, command
import pytest
from redis_business import as_user
from redis_world import RedisWorld

from sova.adapters.redis_keyspace_probes import RedisKeyspaceProbes
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]


@pytest.fixture
def world(redis_cluster_business):
    return RedisWorld(redis_cluster_business)


class TestRedisClusterConformance(HandoffAdapterConformance):
    pass


def test_the_keys_spread_over_every_primary(world):
    source = world.b["admin"]["source"]
    holders = {source.get_node_from_key(k).name
               for k in source.scan_iter(match=world.prefix + "*")}
    assert len(holders) == 3, holders


def test_a_node_the_fence_missed_keeps_it_unconfirmed(world):
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    observation = world.adapter().readback(
        command("redis-keyspace-handoff", "fence_source_writes"), datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    assert observation.evidence["topology"]["nodes_reached"] == 3
    node = world.b["replicas"]["source"][1]  # e.g. an ACL file reloaded on one node
    node.execute_command("ACL", "SETUSER", world.users["batch"], "on", f"~{world.prefix}*",
                         "+@write")
    assert world.adapter().readback(
        command("redis-keyspace-handoff", "fence_source_writes"),
        datetime.now(UTC)).status == EffectStatus.UNKNOWN


def test_the_probes_find_each_keys_node(world):
    b, prefix = world.b, world.prefix
    probes = RedisKeyspaceProbes(
        connect=lambda side: b["admin"][side],
        connect_as=lambda side, name, node=None: as_user(b, side, name, node),
        writers=(world.users["app"], world.users["batch"]), app_user=world.users["app"],
        sets={"sessions": prefix + "session:", "carts": prefix + "cart:"},
        invariants={"CART-01": {"from": prefix + "session:", "field": "cart",
                                "to": prefix + "cart:"}},
        other_prefix="shop-other:")
    passed, live = probes.write_denied("source")
    assert not passed and {o["attempt"] for o in live["outcomes"].values()} == {
        "write_accepted"}
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "verify_final_state", "activate_target"):
        apply(world.adapter(), action)
    for check in (lambda: probes.write_denied("source"), probes.single_writer,
                  lambda: probes.content_equal("sessions"), lambda: probes.invariant("CART-01"),
                  probes.cross_tenant_denied, probes.restore_drill):
        passed, observation = check()
        assert passed, observation
    assert not any(k.startswith(b"{") for k in b["admin"]["target"].scan_iter(match="*"))
