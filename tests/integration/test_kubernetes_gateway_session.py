"""A session server behind a Gateway API entry point (Envoy Gateway) drained
without cutting a session: the rule is left without a backend, so the proxy
refuses new connections while it keeps the upstream of those already open;
the kernel of each source Pod counts them (from the proxy's address) until
they end; then the rule points at the target."""

import threading
import time

from adapter_conformance import apply, command
import pytest
from volume_business import PYTHON, UID

from sova.adapters.kubernetes.gateway_route import GatewayHttpRoute
from sova.adapters.kubernetes_session_handoff import KubernetesSessionHandoffAdapter, SessionSide
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kubernetes
SIDES = {"game_src": ("game-source", 80), "game_tgt": ("game-target", 80)}


def adapter(b, **options):
    controls = b["controls"]()
    route = GatewayHttpRoute(controls, "play", parents=((b["namespace"], "play"),),
                             sides=SIDES, observe=b["observe"], samples=3,
                             converge_seconds=90)
    return KubernetesSessionHandoffAdapter(
        "kubernetes-session-handoff", source=SessionSide(controls, ("deployment", "game-source")),
        target=SessionSide(controls, ("deployment", "game-target")), route=route,
        routes=("game_src", "game_tgt", "paused"), port=8080, image=PYTHON, run_as=(UID, UID),
        **options)


def observe(a, action):
    from datetime import UTC, datetime

    return a.readback(command(a.adapter_id, action), datetime.now(UTC))


def test_matches_through_the_gateway_end_on_the_source_and_new_ones_reach_the_target(
        gateway_session_business):
    b = gateway_session_business
    a = adapter(b, drain_seconds=120)
    apply(a, "stage_target")
    b["clients"]("players", [{"id": f"match-{i}", "seconds": 20} for i in range(2)])
    b["started"]("players")
    pausing = threading.Thread(target=apply, args=(a, "pause_admission"))
    pausing.start()
    deadline = time.monotonic() + 60
    while b["run"]("get", "httproute", "play", "-o",
                   "jsonpath={.spec.rules[0].backendRefs}").strip() not in ("", "[]"):
        assert time.monotonic() < deadline, "the rule kept its backend"
        time.sleep(0.25)
    b["clients"]("late", [{"id": "late", "seconds": 1, "retry": 0}])
    counted = None
    while pausing.is_alive():
        seen = observe(adapter(b, drain_seconds=120), "pause_admission")
        sessions = seen.evidence["sessions"]
        if counted is None and sessions and all("established" in s for s in sessions) and \
                sum(s["established"] for s in sessions) >= 1:
            counted = seen
        pausing.join(1)
    assert counted is not None and counted.status == EffectStatus.UNKNOWN
    done = observe(a, "pause_admission")
    assert done.status == EffectStatus.APPLIED, done.evidence
    for action in ("fence_source_writes", "copy_content", "verify_final_state",
                   "activate_target", "commit_route"):
        apply(a, action)
        assert observe(adapter(b), action).status == EffectStatus.APPLIED, action
    b["clients"]("after", [{"id": "next", "seconds": 2}])
    players, late, after = b["results"]("players"), b["results"]("late"), b["results"]("after")
    assert all(s["completed"] and s["pod"].startswith("game-source")
               for s in players.values()), players
    assert not late["late"]["connected"], late  # the Gateway had no backend for it
    assert after["next"]["completed"] and after["next"]["pod"].startswith("game-target"), after
