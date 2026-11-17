"""The Redis-protocol keyspace adapter (on Valkey) against the provider-neutral
conformance kit, and what an in-memory store adds: expiry times that must
survive the move, a default user that writes everything, and connections
that keep the rights they authenticated with."""

from datetime import UTC, datetime

from adapter_conformance import HandoffAdapterConformance, apply, command, readback
import pytest
from redis_business import as_user
from redis_world import RedisWorld

from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]
HANDOFF = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target", "commit_route")


@pytest.fixture
def world(redis_business):
    return RedisWorld(redis_business)


class TestRedisKeyspaceConformance(HandoffAdapterConformance):
    pass


def test_sessions_expire_on_the_target_when_they_would_have_on_the_source(world):
    b = world.b
    key = f"{world.prefix}session:7"
    expires = b["admin"]["source"].execute_command("PEXPIRETIME", key)
    for action in HANDOFF:
        apply(world.adapter(), action)
    assert b["admin"]["target"].execute_command("PEXPIRETIME", key) == expires
    target = as_user(b, "target", world.users["app"])  # the application, on the target
    assert target.hget(key, "user") == b"u7"
    assert target.xpending(f"{world.prefix}events", "mailer")["pending"] == 5
    assert target.incr(f"{world.prefix}orders:issued") == 21  # no number issued twice
    target.close()


def test_a_connection_opened_before_the_pause_is_ended_by_the_fence(world):
    """Turning a user off refuses new logins only; the fence ends what is open."""
    b = world.b
    early = as_user(b, "source", world.users["app"])
    early.ping()
    apply(world.adapter(), "pause_admission")
    early.set(f"{world.prefix}still-writing", "1")  # still authenticated
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.UNKNOWN
    apply(world.adapter(), "fence_source_writes")
    with pytest.raises(b["redis"].exceptions.RedisError):
        early.set(f"{world.prefix}after-fence", "1")
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED


def test_a_default_user_that_writes_everything_keeps_the_fence_unconfirmed(world):
    b = world.b
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
    b["admin"]["source"].execute_command("ACL", "SETUSER", "default", "on", "nopass", "~*",
                                         "+@all")
    try:
        observation = world.adapter().readback(
            command("redis-keyspace-handoff", "fence_source_writes"), datetime.now(UTC))
        assert observation.status == EffectStatus.UNKNOWN
        assert "default" in observation.evidence["nodes"]["primary"]["other_users_that_may_write"]
    finally:
        b["admin"]["source"].execute_command("ACL", "SETUSER", "default", "off", "resetkeys",
                                             "-@all")
