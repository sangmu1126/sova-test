"""When the target's first records may be left out of a comparison: the
source's time retention removed them after the copy, and nothing else."""

from sova.adapters.kafka_topic_handoff import expired_prefix

DAY = 86_400_000
NOW = 100 * DAY


def rec(offset, value, stamp):
    return (offset, {"key": b"k", "value": value, "headers": [], "timestamp": stamp})


OLD = [rec(i, b"old-%d" % i, NOW - 3 * DAY) for i in range(3)]
NEW = [rec(3 + i, b"new-%d" % i, NOW - 1000) for i in range(4)]
COPIED = [rec(i, r[1]["value"], r[1]["timestamp"]) for i, r in enumerate(OLD + NEW)]


def test_the_records_the_source_expired_after_the_copy_are_left_out():
    assert expired_prefix(NEW, COPIED, DAY, NOW) == 3  # the source now starts at 3
    assert expired_prefix([], [rec(0, b"x", NOW - 3 * DAY)], DAY, NOW) == 1  # all expired


def test_nothing_else_is_left_out():
    assert expired_prefix(OLD + NEW, COPIED, DAY, NOW) == 0  # nothing expired: equal length
    assert expired_prefix(NEW, COPIED, 7 * DAY, NOW) == 0  # not old enough to have gone
    assert expired_prefix(NEW, COPIED, -1, NOW) == 0  # kept forever
    assert expired_prefix(NEW, COPIED, None, NOW) == 0  # retention unknown
    no_time = [(o, {**r, "timestamp": None}) for o, r in COPIED]
    assert expired_prefix(NEW, no_time, DAY, NOW) == 0  # no producer time to judge by
    changed = COPIED[:3] + [rec(3, b"forged", NOW - 1000)] + COPIED[4:]
    assert expired_prefix(NEW, changed, DAY, NOW) == 0  # the rest must equal the source
    from_zero = [rec(i, r[1]["value"], r[1]["timestamp"]) for i, r in enumerate(NEW)]
    assert expired_prefix(from_zero, COPIED, DAY, NOW) == 0  # the source deleted nothing
    assert expired_prefix(COPIED, NEW, DAY, NOW) == 0  # a target missing records: never
