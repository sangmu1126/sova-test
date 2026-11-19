"""The keyspace probes fail where they must: a writer that can still write, a
changed value, a session whose cart is gone, an application user given
another tenant's keys, a scratch database that is not empty."""

from adapter_conformance import apply
import pytest
from redis_business import as_user
from redis_world import RedisWorld
from tls_checks import endpoint, identity_held, summary

from sova.adapters.redis_keyspace_probes import RedisKeyspaceProbes
from sova.adapters.tls_refusal import REDIS_PING, hello_plaintext, transport_refusals

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]


def probes(world):
    b, prefix = world.b, world.prefix
    return RedisKeyspaceProbes(
        connect=lambda side: b["admin"][side],
        connect_as=lambda side, name: as_user(b, side, name),
        writers=(world.users["app"], world.users["batch"]), app_user=world.users["app"],
        sets={"sessions": prefix + "session:", "carts": prefix + "cart:"},
        invariants={"CART-01": {"from": prefix + "session:", "field": "cart",
                                "to": prefix + "cart:"}},
        other_prefix="shop-other:")


def test_each_probe_fails_on_what_it_measures(redis_business):
    world = RedisWorld(redis_business)
    p, b = probes(world), redis_business
    passed, live = p.write_denied("source")
    assert not passed and {w: o["attempt"] for w, o in live["outcomes"].items()} == {
        world.users["app"]: "write_accepted",
        world.users["batch"]: "write_accepted"}  # no transactions, but it can SET
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "verify_final_state", "activate_target"):
        apply(world.adapter(), action)
    for check in (lambda: p.write_denied("source"), p.single_writer,
                  lambda: p.content_equal("sessions"), lambda: p.content_equal("carts"),
                  lambda: p.invariant("CART-01"), p.cross_tenant_denied,
                  lambda: p.restore_drill(15)):
        passed, observation = check()
        assert passed, observation
    target = b["admin"]["target"]
    target.hset(world.prefix + "session:4", "user", "someone else")
    target.delete(world.prefix + "cart:1")
    assert not p.content_equal("sessions")[0]
    passed, invariant = p.invariant("CART-01")
    assert not passed and len(invariant["missing"]) == 5  # every session of cart 1
    target.execute_command("ACL", "SETUSER", world.users["app"], "~shop-other:*")
    passed, isolation = p.cross_tenant_denied()
    assert not passed and isolation["allowed"] == {"GET": True, "SET": True}
    target.set("kept-by-someone", "1")
    target.execute_command("MOVE", "kept-by-someone", 15)
    try:
        passed, drill = p.restore_drill(15)
        assert not passed and "not empty" in drill["reason"]
    finally:
        target.execute_command("SELECT", 0)
        b["redis"].Redis(connection_pool=type(target.connection_pool)(
            connection_class=target.connection_pool.connection_class,
            **{**target.connection_pool.connection_kwargs, "db": 15})).flushdb()


def test_a_writer_that_cannot_log_in_but_keeps_its_keys_is_not_denied(redis_business):
    """Paused, the application user cannot open a connection; the ones it has
    open still write, and the server still permits it: the two views disagree,
    so the probe does not count it as denied (ADR-0007)."""
    world = RedisWorld(redis_business)
    open_ = as_user(redis_business, "source", world.users["app"])
    open_.ping()
    apply(world.adapter(), "pause_admission")
    passed, observation = probes(world).write_denied("source")
    outcome = observation["outcomes"][world.users["app"]]
    assert not passed and outcome == {"attempt": "login_refused", "permitted_on": ["primary"]}
    open_.set(world.prefix + "still-open", "1")  # what the server's view warned of
    open_.close()


def test_unencrypted_transport_is_reported_as_such(redis_business):
    passed, observation = probes(RedisWorld(redis_business)).transport_encrypted("target")
    assert not passed and observation["tls"] is False


def test_valkey_refuses_plaintext_on_its_tls_port_and_serves_it_beside(redis_servers):
    """Valkey resets a plaintext connection on its TLS port, which ends a
    port-forward; the relay brings the forward back for the next check."""
    from forward_relay import ForwardRelay

    relay = ForwardRelay(redis_servers["context"], redis_servers["namespace"],
                         "deployment/valkey-target", 6380)
    plain_port = redis_servers["ports"]["target"]
    try:
        passed, observation = transport_refusals(
            endpoint(relay.port, redis_servers["ca_file"]), plaintext=hello_plaintext(REDIS_PING))
        assert passed and identity_held(observation), summary(observation)
        passed, observation = transport_refusals(
            endpoint(relay.port, redis_servers["ca_file"], plaintext_ports=(plain_port,)),
            plaintext=hello_plaintext(REDIS_PING))
    finally:
        relay.close()
    assert not passed and observation["accepted"] == ["plaintext"]
    by_port = {p["port"]: p["outcome"] for p in observation["checks"]["plaintext"]["ports"]}
    assert by_port == {relay.port: "refused", plain_port: "served"}
