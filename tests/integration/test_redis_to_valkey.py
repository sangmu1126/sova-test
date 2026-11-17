"""A keyspace moved from Redis 8 to Valkey 9, the move many make since Redis
changed its license. Valkey refuses every Redis 8 DUMP (another RDB
version), so the keys go by their contents: the conformance kit holds, and
what DUMP would have carried survives - expiry times, hash fields' own
expiry, sorted-set scores, and streams with their last ID, consumer groups
and pending entries."""

import json

from adapter_conformance import HandoffAdapterConformance, apply, readback
import pytest
from redis_world import RedisWorld

from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]


@pytest.fixture
def world(redis_to_valkey_business):
    return RedisWorld(redis_to_valkey_business)


class TestRedisToValkeyConformance(HandoffAdapterConformance):
    pass


def test_the_target_refuses_the_dump_and_the_contents_carry_everything(world):
    b, prefix = world.b, world.prefix
    source, target = b["admin"]["source"], b["admin"]["target"]
    with pytest.raises(b["redis"].exceptions.ResponseError, match="payload version"):
        target.restore(prefix + "probe", 0, source.dump(prefix + "flag"))
    source.hset(prefix + "profile:1", mapping={"name": "a", "token": "t"})
    source.execute_command("HPEXPIRE", prefix + "profile:1", 3_600_000, "FIELDS", 1, "token")
    source.zadd(prefix + "scores", {"x": 0.1 + 0.2, "y": -1e-300, "z": 1e300})
    source.sadd(prefix + "tags", *[f"t{i}" for i in range(1200)])  # more than one batch
    source.pexpire(prefix + "flag", 7_200_000)
    source.xgroup_create(prefix + "empty-stream", "waiting", id="$", mkstream=True)
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    assert json.loads(target.get(world.adapter().marker))["copied_by"] == "contents"
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert world.content("source") == world.content("target")

    def stream(client):
        events = prefix + "events"
        info = client.xinfo_stream(events)
        groups = client.xinfo_groups(events)
        pending = client.xpending_range(events, "mailer", "-", "+", 100)
        return (info["last-generated-id"], info["entries-added"], info["length"],
                [(g["name"], g["last-delivered-id"], g["pending"], g.get("entries-read"))
                 for g in groups],
                [(p["message_id"], p["consumer"], p["times_delivered"]) for p in pending])

    assert stream(source) == stream(target)
    assert stream(target)[4] and stream(target)[4][0][1] == b"worker-1"  # whose it is
    for key in ("flag", "session:3"):
        assert source.pexpiretime(prefix + key) == target.pexpiretime(prefix + key) > 0
    field = target.execute_command("HPEXPIRETIME", prefix + "profile:1", "FIELDS", 1, "token")
    assert field == source.execute_command("HPEXPIRETIME", prefix + "profile:1", "FIELDS", 1,
                                           "token") and field[0] > 0
    assert target.zscore(prefix + "scores", "x") == 0.1 + 0.2  # the exact double
    assert target.xinfo_groups(prefix + "empty-stream")[0]["name"] == b"waiting"
