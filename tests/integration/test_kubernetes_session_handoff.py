"""A server holding live sessions in memory, handed off without cutting one:
the sessions open at the pause end on the source, connections during the pause
are refused, and new ones after the route reach the target. Counted by the
kernel of each source Pod, not by the server itself."""

from datetime import UTC, datetime
import threading

from adapter_conformance import apply, command
import pytest
from session_business import SIDES
from volume_business import PYTHON, UID

from sova.adapters.kubernetes.handoff_controls import KubernetesServiceRoute
from sova.adapters.kubernetes_session_handoff import KubernetesSessionHandoffAdapter, SessionSide
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kubernetes
HANDOFF = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target", "commit_route")


def adapter(business, **options):
    controls = business["controls"]()
    return KubernetesSessionHandoffAdapter(
        "kubernetes-session-handoff", source=SessionSide(controls, ("deployment", "game-source")),
        target=SessionSide(controls, ("deployment", "game-target")),
        route=KubernetesServiceRoute(controls, "game", SIDES, converge_seconds=60),
        routes=("game_src", "game_tgt", "game_paused"), port=8080, image=PYTHON,
        run_as=(UID, UID), **options)


def observe(a, action):
    return a.readback(command(a.adapter_id, action), datetime.now(UTC))


def test_open_sessions_finish_on_the_source_and_new_ones_reach_the_target(session_business):
    b = session_business
    a = adapter(b, drain_seconds=120)
    for action in ("stage_target",):
        apply(a, action)
    b["clients"]("players", [{"id": f"match-{i}", "seconds": 20} for i in range(3)])
    b["started"]("players")
    assert observe(a, "pause_admission").status == EffectStatus.UNKNOWN
    pausing = threading.Thread(target=apply, args=(a, "pause_admission"))
    pausing.start()
    b["run"]("wait", "--for=jsonpath={.spec.selector.side}=paused", "service/game",
             "--timeout=60s")
    b["clients"]("late", [{"id": "late", "seconds": 1}])
    mid = None
    while pausing.is_alive():
        seen = observe(adapter(b, drain_seconds=120), "pause_admission")
        counted = seen.evidence["sessions"]
        if mid is None and counted and all("established" in c for c in counted):
            mid = seen
        pausing.join(1)
    # While the matches were still on, the pause was not done: they were counted.
    assert mid is not None and mid.status == EffectStatus.UNKNOWN
    assert sum(s.get("established", 0) for s in mid.evidence["sessions"]) >= 1
    done = observe(a, "pause_admission")
    assert done.status == EffectStatus.APPLIED, done.evidence
    assert done.evidence["sessions_to_terminate"] == 0
    for action in HANDOFF[2:]:
        apply(a, action)
        assert observe(adapter(b), action).status == EffectStatus.APPLIED, action
    b["clients"]("after", [{"id": f"new-{i}", "seconds": 2} for i in range(2)])
    players, late, after = b["results"]("players"), b["results"]("late"), b["results"]("after")
    assert all(s["completed"] and s["pod"].startswith("game-source") for s in players.values()), \
        players
    assert not late["late"]["connected"]  # refused while admission was paused
    assert all(s["completed"] and s["pod"].startswith("game-target") for s in after.values()), \
        after


def test_a_drain_past_its_bound_holds_or_reports_what_the_fence_will_cut(session_business):
    b = session_business
    b["clients"]("long", [{"id": "long", "seconds": 90}])
    b["started"]("long")
    holding = adapter(b, drain_seconds=5)
    apply(holding, "pause_admission")  # returns at the bound, the session still open
    seen = observe(holding, "pause_admission")
    assert seen.status == EffectStatus.UNKNOWN and seen.evidence["deadline_passed"]
    counts = sorted(s["established"] for s in seen.evidence["sessions"] if "established" in s)
    assert counts == [0, 1], seen.evidence  # two source Pods, the match on one of them
    cutting = adapter(b, drain_seconds=5, on_timeout="terminate")
    seen = observe(cutting, "pause_admission")
    assert seen.status == EffectStatus.APPLIED and seen.evidence["sessions_to_terminate"] == 1
    apply(cutting, "fence_source_writes")
    assert observe(cutting, "fence_source_writes").status == EffectStatus.APPLIED
    long = b["results"]("long")["long"]
    assert long["connected"] and not long["completed"]  # cut by the fence, as reported


def test_a_source_started_again_reads_back_as_not_fenced(session_business):
    b = session_business
    a = adapter(b, drain_seconds=30)
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(a, action)
    assert observe(a, "fence_source_writes").status == EffectStatus.APPLIED
    b["run"]("scale", "deployment/game-source", "--replicas=1")
    assert observe(a, "fence_source_writes").status == EffectStatus.UNKNOWN
