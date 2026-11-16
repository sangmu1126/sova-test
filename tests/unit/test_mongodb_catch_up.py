"""When the change-stream catch-up stops, against a scripted stream: it must
stop on a source that keeps serving, not stop inside a transaction, wait out
an empty batch that is behind, give up at its deadline, and refuse a
structural change."""

import pytest

from sova.adapters.mongodb_change_stream import MongoChangeStreamHandoffAdapter
from sova.domain.contracts import ContractError


def token(seconds, increment=0):
    return {"_data": f"82{seconds:08x}{increment:08x}2b0229"}


class Stream:
    """``script`` items: an event ``(seconds, increment, kind)`` or ``("empty", seconds)``;
    after the script, ``tail(n)`` gives the endless rest."""

    def __init__(self, script, tail=None):
        self.script, self.tail, self.n, self.resume_token = list(script), tail, 0, token(1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def try_next(self):
        if self.script:
            item = self.script.pop(0)
        elif self.tail:
            item = self.tail(self.n)
        else:
            item = ("empty", 0)
        self.n += 1
        if item[0] == "empty":
            if item[1]:
                self.resume_token = token(item[1])
            return None
        seconds, increment, kind = item
        self.resume_token = token(seconds, increment)
        return {"operationType": kind, "at": (seconds, increment)}


class Scripted(MongoChangeStreamHandoffAdapter):
    def __init__(self, stream, **kw):
        class Route:
            def get(self):
                return None

            def set(self, value):
                pass

        super().__init__("mongodb-cdc", connect=lambda side: {"orders_new": None},
                         source_db="orders", target_db="orders_new", collections=("orders",),
                         writers=("app@admin",), app_user="app@admin",
                         admin_user="sova-admin@admin", route=Route(), **kw)
        self.stream, self.applied = stream, []

    def _stream(self, token=None, at=None):
        return self.stream

    def _apply(self, target, event):
        if event["operationType"] == "drop":
            return super()._apply(target, event)
        self.applied.append(event["at"])

    def _keep(self, token, mode, **detail):
        pass


def test_a_source_that_keeps_serving_is_not_chased():
    adapter = Scripted(Stream([], tail=lambda n: (100 + n, 0, "insert")), catch_up_seconds=5)
    _, applied = adapter._drain(token(1), (105, 0))
    assert adapter.applied[-1] == (106, 0) and applied == 7  # stopped once past it


def test_the_events_of_one_transaction_are_all_applied():
    # A transaction's events all carry its commit time: stopping at the first
    # one that reaches the point would leave the rest behind.
    script = [(100, 0, "insert"), (105, 3, "update"), (105, 3, "insert"), (105, 3, "delete"),
              ("empty", 106)]
    adapter = Scripted(Stream(script), catch_up_seconds=5)
    _, applied = adapter._drain(token(1), (105, 3))
    assert applied == 4 and adapter.stream.script == []


def test_an_empty_batch_behind_the_point_is_waited_out():
    script = [("empty", 90), (95, 0, "insert"), ("empty", 99), ("empty", 105)]
    adapter = Scripted(Stream(script), catch_up_seconds=5)
    _, applied = adapter._drain(token(1), (105, 0))
    assert applied == 1 and adapter.stream.script == []


def test_a_stream_that_never_reaches_the_point_gives_up():
    adapter = Scripted(Stream([], tail=lambda n: ("empty", 50)), catch_up_seconds=0.2)
    with pytest.raises(ContractError, match="did not reach"):
        adapter._drain(token(1), (105, 0))
    busy = Scripted(Stream([], tail=lambda n: (50, n, "insert")), catch_up_seconds=0.2)
    with pytest.raises(ContractError, match="did not reach"):  # events, but none past it
        busy._drain(token(1), (105, 0))


def test_a_structural_change_ends_the_sync():
    adapter = Scripted(Stream([(100, 0, "drop")]))
    with pytest.raises(ContractError, match="drop"):
        adapter._drain(token(1), (105, 0))
