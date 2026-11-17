"""When a key or hash field counts as falling due, and how each field's expiry
is read: in batches, and as "none" on a server without field expiry."""

import pytest

from sova.adapters.redis_keyspace_handoff import _BATCH, due, field_expiries

redis = pytest.importorskip("redis")


def test_due_is_an_expiry_up_to_the_cut_or_one_already_passed():
    cut = 10_000
    assert due(10_000, cut) and due(1, cut) and due(-2, cut)
    assert not due(10_001, cut) and not due(-1, cut) and not due(0, cut)


class Fake:
    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error = [], reply, error

    def execute_command(self, *args):
        self.calls.append(args)
        if self.error:
            raise redis.exceptions.ResponseError(self.error)
        count = args[3]
        return [self.reply] * count


def test_field_expiries_are_read_in_batches():
    fields = [f"f{i}".encode() for i in range(_BATCH * 2 + 3)]
    fake = Fake(reply=123)
    assert field_expiries(fake, b"k", fields) == [123] * len(fields)
    assert [c[3] for c in fake.calls] == [_BATCH, _BATCH, 3]
    assert all(c[:3] == ("HPEXPIRETIME", b"k", "FIELDS") for c in fake.calls)


def test_a_server_without_field_expiry_has_none_to_compare():
    fake = Fake(error="ERR unknown command 'HPEXPIRETIME', with args beginning with: ")
    assert field_expiries(fake, b"k", [b"a", b"b"]) == [-1, -1]


def test_any_other_refusal_is_not_taken_for_no_expiry():
    with pytest.raises(redis.exceptions.ResponseError):
        field_expiries(Fake(error="NOPERM no permission"), b"k", [b"a"])
