"""A Redis-protocol keyspace whose servers a Sentinel watches. A primary counts
only the replicas connected now; a replica cut off while the fence runs keeps
its old ACLs and may come back and be promoted. Sentinel remembers it, so
its view must agree with the registered topology."""

from datetime import UTC, datetime
import time

from adapter_conformance import apply, command
import pytest
from redis_business import ADMIN
from redis_world import Route

from sova.adapters.redis_keyspace_handoff import RedisKeyspaceHandoffAdapter
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]


def adapter(b, *, registered=True, watched=True, master="sova-source"):
    users = b["users"]
    names = {"source": master, "target": "sova-target"}
    return RedisKeyspaceHandoffAdapter(
        "redis-keyspace-handoff", connect=lambda side: b["admin"][side],
        prefixes=(b["prefix"] + "*",), writers=(users["app"], users["batch"]),
        app_user=users["app"], admin_user=ADMIN[0], route=Route("source"),
        routes=("source", "target"),
        replicas=(lambda side: b["replicas"][side]) if registered else None,
        sentinel=(lambda side: ([b["sentinel"]], names[side])) if watched else None)


def fenced(b, **kw):
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(adapter(b, **kw), action)
    return adapter(b, **kw).readback(command("redis-keyspace-handoff", "fence_source_writes"),
                                     datetime.now(UTC))


def linked(client, up, seconds=60):
    deadline = time.monotonic() + seconds
    while (client.info("replication").get("master_link_status") == "up") != up:
        assert time.monotonic() < deadline, f"replica link not {'up' if up else 'down'}"
        time.sleep(0.3)


def test_the_fence_is_confirmed_when_sentinel_agrees(redis_sentinel_business):
    observation = fenced(redis_sentinel_business)
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    view = observation.evidence["topology"]["sentinel"][0]
    assert view["master_is_this_primary"] and view["unregistered"] == []


def test_a_replica_cut_off_during_the_fence_keeps_it_unconfirmed(redis_sentinel_business):
    b = redis_sentinel_business
    replica, primary = b["replicas"]["source"][0], b["admin"]["source"]
    replica.config_set("masterauth", "not-the-password")  # it cannot come back for now
    replica.execute_command("CLIENT", "KILL", "TYPE", "master")
    try:
        linked(replica, up=False)
        deadline = time.monotonic() + 30
        while primary.info("replication").get("connected_slaves", 0):
            assert time.monotonic() < deadline
            time.sleep(0.3)
        # Counting connected replicas alone sees none, so nothing to register:
        alone = fenced(b, registered=False, watched=False)
        assert alone.status == EffectStatus.APPLIED, alone.evidence
        # ... while the cut-off replica still lets the writer in, as the fence never
        # reached it. Sentinel remembers it:
        assert replica.execute_command("ACL", "DRYRUN", b["users"]["batch"], "SET",
                                       b["prefix"] + "x", "1") == b"OK"
        watched = fenced(b, registered=False)
        assert watched.status == EffectStatus.UNKNOWN
        view = watched.evidence["topology"]["sentinel"][0]
        assert view["replicas_known"] == 1 and len(view["unregistered"]) == 1, view
    finally:
        replica.config_set("masterauth", ADMIN[1])
        replica.execute_command("CLIENT", "KILL", "TYPE", "master")
        linked(replica, up=True)


def test_a_primary_sentinel_does_not_name_keeps_it_unconfirmed(redis_sentinel_business):
    observation = fenced(redis_sentinel_business, master="sova-target")  # the other master
    assert observation.status == EffectStatus.UNKNOWN
    assert not observation.evidence["topology"]["sentinel"][0]["master_is_this_primary"]
