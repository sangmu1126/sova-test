"""The worker ends an execution once the steps it runs in a row are done - all
of them, a blocked state, or a step that fails, even one that failed right
after building it - and recovery ends the one it builds for its readback.
Helpers an execution keeps between steps (volume agents) are let go there."""

import threading
from types import SimpleNamespace

import pytest

from sova.domain.handoff_authority import HandoffState
from sova.service import worker as worker_module
from sova.service.worker import Execution, HandoffWorker, close_execution

OPS = [{"operation_id": f"op-{i}", "adapter": {"adapter_id": "a"}, "required_evidence": []}
       for i in range(3)]


def stand_in(monkeypatch, step):
    """A worker whose store walks the three operations and whose step is ``step``."""
    w = object.__new__(HandoffWorker)
    w.stopping, w.lease_seconds, w.max_step_seconds = threading.Event(), 30, 60
    at = {"next": 0}
    store = SimpleNamespace(current=lambda app, unit: SimpleNamespace(
        state=HandoffState.SOURCE_FENCED, next_operation=at["next"]))
    ownership = SimpleNamespace(renew=lambda lease, lease_seconds: lease)
    monkeypatch.setattr(HandoffWorker, "_step", step(at))

    def run():
        return w._run("tenant", "app", "unit", None, None, 1, {"obligations": [],
                      "operations": OPS}, "handoff-1", ownership,
                      SimpleNamespace(operation_id="handoff-1"), None, store)
    return run


def execution(ended, name):
    return Execution(adapter=None, measure=None, close=lambda: ended.append(name))


def test_one_execution_serves_the_steps_in_a_row_and_is_ended_once(monkeypatch):
    ended = []

    def step(at):
        def run(self, *args):
            execution_, opened = args[-2], args[-1]
            if execution_ is None:
                execution_ = execution(ended, "built")
                opened.append(execution_)
            at["next"] += 1
            return SimpleNamespace(state=HandoffState.SOURCE_FENCED), None, execution_, False
        return run

    stand_in(monkeypatch, step)()
    assert ended == ["built"]


@pytest.mark.parametrize("built_before", [False, True])
def test_a_step_that_fails_still_ends_the_execution(monkeypatch, built_before):
    ended = []

    def step(at):
        def run(self, *args):
            execution_, opened = args[-2], args[-1]
            if execution_ is None:
                execution_ = execution(ended, "built")
                opened.append(execution_)
                if not built_before:
                    raise RuntimeError("the step that built it failed")
                at["next"] += 1
                return None, None, execution_, False
            raise RuntimeError("a later step failed")
        return run

    with pytest.raises(RuntimeError):
        stand_in(monkeypatch, step)()
    assert ended == ["built"]


def test_ending_an_execution_never_raises_over_the_outcome(caplog):
    def broken():
        raise OSError("the cluster is gone")
    close_execution(Execution(adapter=None, measure=None, close=broken))
    close_execution(Execution(adapter=None, measure=None))  # nothing to end
    close_execution(None)
    assert "ending an execution failed" in caplog.text


def test_a_composite_ends_each_component_whatever_another_does():
    from sova.service.composite_execution import composite_execution

    ended = []

    def broken():
        ended.append("volume")
        raise OSError("gone")
    def adapter(adapter_id):
        return SimpleNamespace(adapter_id=adapter_id, validate=None, apply=None, readback=None)
    parts = [("volume", Execution(adapter("v"), None, close=broken)),
             ("session", Execution(adapter("s"), None, close=lambda: ended.append("session")))]
    composite_execution("unit", parts, data="volume").close()
    assert ended == ["volume", "session"]
    assert worker_module.close_execution is close_execution
