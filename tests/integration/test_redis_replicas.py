"""A Redis-protocol keyspace whose servers have replicas. ACL users are kept
per node and are not replicated, so a fence that reached only the primary
would let a writer write again on a replica a failover promotes. Measured
here:
* the fence reaches every registered replica, and holds after a failover;
* activation reaches the target's replicas;
* a replica the fence does not cover keeps the fence unconfirmed: a changed
  replica ACL, or a replica the primary reports that is not registered;
* the write probe asks every node."""

from adapter_conformance import apply, command, readback
from datetime import UTC, datetime
import time
import pytest
from redis_business import ADMIN, PASSWORD
from redis_world import Route

from sova.adapters.redis_keyspace_handoff import RedisKeyspaceHandoffAdapter
from sova.adapters.redis_keyspace_probes import RedisKeyspaceProbes
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]
FORWARD = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target")


def adapter(b, *, registered=True):
    users = b["users"]
    return RedisKeyspaceHandoffAdapter(
        "redis-keyspace-handoff", connect=lambda side: b["admin"][side],
        prefixes=(b["prefix"] + "*",), writers=(users["app"], users["batch"]),
        app_user=users["app"], admin_user=ADMIN[0], route=Route("source"),
        routes=("source", "target"),
        replicas=(lambda side: b["replicas"][side]) if registered else None)


def evidence(b, action, **kw):
    return adapter(b, **kw).readback(command("redis-keyspace-handoff", action),
                                     datetime.now(UTC))


def may_set(client, b, name):
    return client.execute_command("ACL", "DRYRUN", name, "SET", b["prefix"] + "x", "1") == b"OK"


def is_on(client, name):
    return "on" in client.acl_getuser(name)["flags"]


def login(b, port, name):
    return b["redis"].Redis(host="127.0.0.1", port=port, username=name, password=PASSWORD,
                            socket_timeout=10)


def test_the_fence_reaches_every_replica_and_holds_after_a_failover(redis_replicated_business):
    b = redis_replicated_business
    users = b["users"]
    for action in FORWARD:
        apply(adapter(b), action)
        assert readback(adapter(b), action) == EffectStatus.APPLIED, action
    replica = b["replicas"]["source"][0]
    for name in (users["app"], users["batch"]):
        assert not may_set(replica, b, name)
    target_replica = b["replicas"]["target"][0]  # promoted, it serves the application
    assert may_set(target_replica, b, users["app"]) and is_on(target_replica, users["app"])
    assert not is_on(target_replica, users["batch"])  # staged with its rules, left off
    # The source's primary fails and its replica is promoted: the writers stay out.
    replica.execute_command("REPLICAOF", "NO", "ONE")
    try:
        for name in (users["app"], users["batch"]):
            with pytest.raises(b["redis"].exceptions.AuthenticationError):
                login(b, b["replica_ports"]["source"][0], name).ping()
    finally:  # back under the primary, and caught up, for the next test
        replica.execute_command("REPLICAOF", "valkey-source", "6379")
        deadline = time.monotonic() + 60
        while replica.info("replication").get("master_link_status") != "up":
            assert time.monotonic() < deadline, "the replica did not rejoin its primary"
            time.sleep(0.3)


def test_a_replica_whose_acl_lets_a_writer_back_in_keeps_the_fence_unconfirmed(
        redis_replicated_business):
    b = redis_replicated_business
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(adapter(b), action)
    assert readback(adapter(b), "fence_source_writes") == EffectStatus.APPLIED
    replica = b["replicas"]["source"][0]  # e.g. an ACL file reloaded on that node
    replica.execute_command("ACL", "SETUSER", b["users"]["batch"], "on", f"~{b['prefix']}*",
                            "+@write")
    observation = evidence(b, "fence_source_writes")
    assert observation.status == EffectStatus.UNKNOWN
    state = observation.evidence["nodes"]["replica-0"]["writers"][b["users"]["batch"]]
    assert state["on"] and all(state["may_write"].values()), state
    probes = RedisKeyspaceProbes(
        connect=lambda side: b["admin"][side],
        connect_as=lambda side, name: login(b, b["ports"][side], name),
        writers=(b["users"]["app"], b["users"]["batch"]), app_user=b["users"]["app"],
        sets={"sessions": b["prefix"] + "session:"}, replicas=lambda side: b["replicas"][side])
    passed, observation = probes.write_denied("source")
    batch = observation["outcomes"][b["users"]["batch"]]
    assert not passed and batch == {"attempt": "login_refused", "permitted_on": ["replica-0"]}


def test_a_replica_that_is_not_registered_keeps_the_fence_unconfirmed(
        redis_replicated_business):
    b = redis_replicated_business
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(adapter(b, registered=False), action)
    observation = evidence(b, "fence_source_writes", registered=False)
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["topology"]["replicas_reported"] == 1
    assert observation.evidence["topology"]["replicas_registered"] == 0
    # What that would have meant: the unreached replica still lets the writer in.
    assert may_set(b["replicas"]["source"][0], b, b["users"]["batch"])
