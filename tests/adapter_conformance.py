"""Conformance kit every handoff data adapter must pass, whatever it moves.

A handoff adapter performs the external effects of a compiled plan (stage the
target, pause admission, fence the source, copy, activate, switch the route)
and reads each postcondition back from the real systems. The control plane's
safety rests on properties that do not depend on the provider, so they are
checked here once, against any adapter, through a provider-specific *world*:

* readback never changes the systems it reads;
* readback depends on the systems only: an adapter instance that never ran
  the action (as after a lost response or a restarted worker) reaches the
  same verdict;
* applying an action twice has the effect of applying it once;
* a postcondition someone undoes outside the plan reads back as UNKNOWN;
* a write path left pending before the fence (a prepared transaction, an
  incomplete multipart upload) keeps the fence unconfirmed until resolved;
* records removed on the source before the final copy are gone on the target;
* only the application writer is activated on the target, every other
  registered writer stays denied, and the source never reopens;
* evidence names the action and stays bounded.

A provider test module subclasses ``HandoffAdapterConformance`` and supplies a
``world`` fixture with:

``adapter()`` / ``reverse_adapter()``
    a fresh adapter for the forward handoff / the failback direction;
``can_write(side, writer)``
    an independent attempt to write as ``"app"`` or ``"other"`` (a second
    registered writer) that never leaves data behind;
``business_write(count)`` / ``business_delete(count)``
    the application writes ``count`` new records on the source, or removes
    ``count`` existing ones;
``content(side)``
    an independent digest of the business content, ``None`` if absent;
``state()``
    everything observable (content, access, pending writes, route);
``reopen_source()`` / ``tamper_target()`` / ``move_route(side)``
    changes made outside the plan;
``open_pending_write()`` / ``resolve_pending_write()``
    a write path that outlives its client, and the operator resolving it;
``route()`` / ``route_of(side)``
    the current route and the route value of a side.
"""

from datetime import UTC, datetime, timedelta
import json

import pytest

from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

# The adapter-bound steps of the two forward templates, in plan order.
SEQUENCES = {
    "offline-copy": ("stage_target", "pause_admission", "fence_source_writes",
                     "copy_content", "verify_final_state", "activate_target", "commit_route"),
    "snapshot-cdc": ("stage_target", "start_sync", "pause_admission", "fence_source_writes",
                     "apply_final_changes", "verify_final_state", "activate_target",
                     "commit_route"),
}
ACTIONS = tuple(dict.fromkeys(a for steps in SEQUENCES.values() for a in steps))
VERIFY_ONLY = frozenset({"verify_final_state", "verify_return_target"})


def command(adapter_id, action, *, step=None):
    now = datetime.now(UTC)
    return EffectCommand(
        ResourceScope("tenant-conformance", "app-conformance", "environment-conformance",
                      "unit-conformance"),
        "operation-conformance", step or action, adapter_id, action,
        f"operation-conformance:{step or action}", "c" * 64, "revision-1", 1,
        now + timedelta(minutes=10), json.dumps({"conformance": True}))


def readback(adapter, action):
    cmd = command(adapter.adapter_id, action)
    observation = adapter.readback(cmd, datetime.now(UTC))
    evidence = observation.evidence
    assert observation.scope == cmd.scope
    assert observation.command_digest == cmd.fingerprint
    assert evidence["action"] == action
    assert len(observation.evidence_json) <= 65536
    return observation.status


def apply(adapter, action):
    cmd = command(adapter.adapter_id, action)
    adapter.validate(cmd)
    adapter.apply(cmd)


class HandoffAdapterConformance:
    """Provider-neutral properties; subclasses provide the ``world`` fixture."""

    def test_rejects_foreign_adapters_and_unknown_actions(self, world):
        adapter = world.adapter()
        with pytest.raises(ContractError):
            adapter.validate(command("another-adapter", "copy_content"))
        with pytest.raises(ContractError):
            adapter.validate(command(adapter.adapter_id, "drop_everything"))

    def test_nothing_reads_back_as_done_before_it_is_done(self, world):
        before = world.state()
        for action in ACTIONS:
            if action not in VERIFY_ONLY:
                assert readback(world.adapter(), action) == EffectStatus.UNKNOWN, action
        assert world.state() == before  # reading changed nothing

    @pytest.mark.parametrize("strategy", sorted(SEQUENCES))
    def test_forward_handoff_holds_every_property(self, world, strategy):
        for action in SEQUENCES[strategy]:
            before = world.state()
            if action not in VERIFY_ONLY:
                assert readback(world.adapter(), action) == EffectStatus.UNKNOWN, action
            assert world.state() == before, f"readback of {action} changed the systems"
            apply(world.adapter(), action)
            # A different instance, which never ran the action, confirms it.
            assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
            once = world.state()
            apply(world.adapter(), action)
            assert world.state() == once, f"{action} is not idempotent"
            assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
            if action in VERIFY_ONLY:
                assert once == before, f"verification {action} changed the systems"
            self._semantics(world, action)

    def _semantics(self, world, action):
        if action == "stage_target":
            assert not world.can_write("target", "app")
            assert not world.can_write("target", "other")
        elif action == "start_sync":
            # The source still serves the business while the first copy runs:
            # new records and removed ones must both reach the target.
            assert world.can_write("source", "app")
            world.business_write(3)
            world.business_delete(1)
        elif action == "pause_admission":
            assert not world.can_write("source", "app")
        elif action == "fence_source_writes":
            assert not world.can_write("source", "app")
            assert not world.can_write("source", "other")
        elif action in ("copy_content", "apply_final_changes", "verify_final_state"):
            assert world.content("source") == world.content("target") is not None
        elif action == "activate_target":
            assert world.can_write("target", "app")
            assert not world.can_write("target", "other")  # only the application
            assert not world.can_write("source", "app")
            assert not world.can_write("source", "other")
        elif action == "commit_route":
            assert world.route() == world.route_of("target")

    def _run(self, world, through):
        steps = SEQUENCES["snapshot-cdc"]
        for action in steps[:steps.index(through) + 1]:
            if action == "start_sync":
                apply(world.adapter(), action)
                world.business_write(2)
                continue
            apply(world.adapter(), action)

    def test_an_undone_fence_reads_back_unknown(self, world):
        self._run(world, "fence_source_writes")
        assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
        world.reopen_source()  # e.g. restored from a backup with the old grants
        assert readback(world.adapter(), "fence_source_writes") == EffectStatus.UNKNOWN
        assert world.can_write("source", "app")  # the world really reopened

    def test_changed_target_content_reads_back_unknown(self, world):
        self._run(world, "verify_final_state")
        world.tamper_target()
        assert readback(world.adapter(), "verify_final_state") == EffectStatus.UNKNOWN

    def test_a_moved_route_reads_back_unknown(self, world):
        self._run(world, "commit_route")
        world.move_route("source")
        assert readback(world.adapter(), "commit_route") == EffectStatus.UNKNOWN

    def test_a_pending_write_path_keeps_the_fence_unconfirmed(self, world):
        self._run(world, "start_sync")
        world.open_pending_write()
        for action in ("pause_admission", "fence_source_writes"):
            apply(world.adapter(), action)
        assert readback(world.adapter(), "fence_source_writes") == EffectStatus.UNKNOWN
        world.resolve_pending_write()  # the operator decides, not the adapter
        assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED

    def test_failback_return_target_is_the_fenced_original_source(self, world):
        self._run(world, "commit_route")
        assert readback(world.reverse_adapter(), "verify_return_target") == \
            EffectStatus.APPLIED
        world.reopen_source()
        assert readback(world.reverse_adapter(), "verify_return_target") == \
            EffectStatus.UNKNOWN
