"""MySQL change capture between servers whose ``gtid_mode`` differs. The
source starts with GTIDs off (MySQL's own default) and steps toward on
online; the target has them on, and at the end off.

A target with GTIDs on refuses an anonymous transaction: the channel gives
each one a GTID under a UUID of its own, which the target's
``gtid_executed`` then shows. A target with GTIDs off cannot take a GTID
transaction at all: refused before anything is copied."""

from datetime import UTC, datetime

from adapter_conformance import apply, command, readback
import pytest
from mysql_pair_world import MySqlPairWorld

from sova.domain.contracts import ContractError, EffectStatus

pytestmark = [pytest.mark.mysql, pytest.mark.kubernetes]


def on(business, side, query):
    pymysql = business["pymysql"]
    with pymysql.connect(**business["admins"][side]) as conn, conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()


def step_gtid_mode(business, side, *modes):
    for mode in modes:  # one step at a time, as the server allows it online
        on(business, side, f"SET @@GLOBAL.gtid_mode = {mode}")


def evidence(world, action):
    cmd = command(world.adapter().adapter_id, action)
    return world.adapter().readback(cmd, datetime.now(UTC)).evidence


def hand_off(world):
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "start_sync") == EffectStatus.APPLIED
    seen = evidence(world, "start_sync")
    world.business_write(5)
    world.business_delete(2)
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert world.content("source") == world.content("target")
    return seen


def executed_under(business, uuid):
    executed = on(business, "target", "SELECT @@GLOBAL.gtid_executed")[0][0]
    return uuid in executed.replace("\n", "")


@pytest.mark.parametrize("source_mode", ["OFF", "OFF_PERMISSIVE", "ON_PERMISSIVE"])
def test_a_source_that_writes_anonymous_transactions_is_captured(mysql_gtid_off_business,
                                                                  source_mode):
    business = mysql_gtid_off_business
    current = on(business, "source", "SELECT @@GLOBAL.gtid_mode")[0][0]
    path = ["OFF", "OFF_PERMISSIVE", "ON_PERMISSIVE"]
    step_gtid_mode(business, "source", *path[path.index(current) + 1:path.index(source_mode) + 1])
    world = MySqlPairWorld(business)
    seen = hand_off(world)
    assert seen["gtid_modes"] == {"source": source_mode, "target": "ON"}
    assert seen["gtids_assigned"] == {"type": "UUID", "value": world.adapter().assigned_uuid}
    if source_mode == "ON_PERMISSIVE":  # its own transactions keep the source's GTIDs
        source_uuid = on(business, "source", "SELECT @@GLOBAL.server_uuid")[0][0]
        assert executed_under(business, source_uuid)
    else:
        assert executed_under(business, world.adapter().assigned_uuid)


def test_a_target_without_gtids_is_refused_a_source_that_writes_them(mysql_gtid_off_business):
    business = mysql_gtid_off_business
    assert on(business, "source", "SELECT @@GLOBAL.gtid_mode")[0][0] == "ON_PERMISSIVE"
    step_gtid_mode(business, "target", "ON_PERMISSIVE", "OFF_PERMISSIVE", "OFF")
    try:
        world = MySqlPairWorld(business)
        apply(world.adapter(), "stage_target")
        with pytest.raises(ContractError, match="OFF_PERMISSIVE or ON_PERMISSIVE"):
            apply(world.adapter(), "start_sync")
        assert world.adapter()._status() is None  # nothing started
        seen = evidence(world, "start_sync")
        assert seen["gtids_compatible"] is False
        assert readback(world.adapter(), "start_sync") != EffectStatus.APPLIED
    finally:
        step_gtid_mode(business, "target", "OFF_PERMISSIVE", "ON_PERMISSIVE", "ON")
