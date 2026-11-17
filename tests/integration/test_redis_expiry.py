"""A session store expires keys all the time. One comparison reads the source,
then the target; each server expires a key at the same absolute time, but
the reads are not at the same time, so keys falling due in between would be
on one side only and hold a correct handoff. Keys and hash fields due
within the margin are left out of both sides and counted; the rest,
including each hash field's own expiry, must be equal."""

from adapter_conformance import apply, readback
import pytest
from redis_world import RedisWorld
from test_redis_keyspace_probes import probes

from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]


def server_ms(client):
    seconds, micros = client.time()
    return seconds * 1000 + micros // 1000


def test_keys_and_fields_falling_due_during_the_comparison_do_not_hold_it(redis_business):
    world, admin = RedisWorld(redis_business), redis_business["admin"]["source"]
    prefix, now = world.prefix, server_ms(admin)
    pipe = admin.pipeline(transaction=False)
    for i in range(600):  # one falls due every 20 ms, from 4 s to 16 s from now
        key = f"{prefix}session:due-{i}"
        pipe.hset(key, mapping={"user": f"due-{i}", "cart": "0"})
        pipe.execute_command("PEXPIREAT", key, now + 4000 + i * 20)
    fields = f"{prefix}cart:fields"
    pipe.hset(fields, mapping={"soon": "1", "late": "2", "kept": "3"})
    pipe.execute_command("HPEXPIREAT", fields, now + 6000, "FIELDS", 1, "soon")
    pipe.execute_command("HPEXPIREAT", fields, now + 3_600_000, "FIELDS", 1, "late")
    pipe.execute()
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    target = redis_business["admin"]["target"]
    assert target.execute_command("HPEXPIRETIME", fields, "FIELDS", 1, "late") == [
        now + 3_600_000]  # each field's own expiry copied
    p, rounds = probes(world), 0
    while server_ms(admin) < now + 17000:  # compare throughout
        assert readback(world.adapter(), "copy_content") == EffectStatus.APPLIED
        for name in ("sessions", "carts"):
            passed, seen = p.content_equal(name)
            assert passed, seen
        rounds += 1
    assert rounds >= 3, rounds
    # A field's expiry beyond the margin, changed on the target, is found.
    target.execute_command("HPEXPIREAT", fields, now + 7_200_000, "FIELDS", 1, "late")
    assert readback(world.adapter(), "copy_content") != EffectStatus.APPLIED
    passed, seen = p.content_equal("carts")
    assert not passed and seen["source"]["digest"] != seen["target"]["digest"]


def test_what_falls_due_within_the_margin_is_counted_not_compared(redis_business):
    world, admin = RedisWorld(redis_business), redis_business["admin"]["source"]
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    key = f"{world.prefix}session:3"
    later = server_ms(admin) + 60_000  # within the default 120 s margin
    for side in ("source", "target"):
        redis_business["admin"][side].execute_command("PEXPIREAT", key, later)
    passed, seen = probes(world).content_equal("sessions")
    assert passed and seen["source"]["left_out_expiring"] == 1, seen
    redis_business["admin"]["target"].delete(key)  # not compared: it falls due anyway
    assert probes(world).content_equal("sessions")[0]
