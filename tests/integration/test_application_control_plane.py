"""The demo's shape, handed off whole by the control plane: a board that keeps
posts in SQLite and uploads on a volume, and a game server that keeps matches
in memory, as one unit with one write stop. A match running when the handoff
starts ends normally on the source; the board's data arrives intact; new
matches and new posts go to the target; every obligation is measured and
signed.

The game is reached through a Service or through a Gateway API entry point
(Envoy Gateway). Where the cluster's Service dataplane drops open flows once
the selector moves (``SOVA_TEST_ROUTE_FLOWS=dropped``: GKE Dataplane V2,
measured) staging refuses the Service route, and the Gateway one carries the
matches."""

import json
import os

from control_plane_support import board_contract, prepared, signed_results, worker_for
import pytest
from session_business import SIDES
from test_volume_control_plane import board_settings
from volume_business import PYTHON, UID
from volume_world import VolumeWorld

from sova.adapters.kubernetes.gateway_route import GatewayHttpRoute
from sova.adapters.kubernetes.handoff_controls import KubernetesServiceRoute
from sova.adapters.kubernetes_session_handoff import SessionSide
from sova.domain.handoff_authority import HandoffState
from sova.service.composite_execution import composite_execution, session_execution
from sova.service.volume_execution import volume_factory

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes]


FLOWS_DROPPED = os.environ.get("SOVA_TEST_ROUTE_FLOWS") == "dropped"
GATEWAY_SIDES = {"game_src": ("game-source", 80), "game_tgt": ("game-target", 80)}


def game_entry(entry, game):
    """The game's route: its Service's selector, or the Gateway's HTTPRoute."""
    controls = game["controls"]()
    if entry == "service":
        return controls, KubernetesServiceRoute(controls, "game", SIDES, converge_seconds=60), (
            "game_src", "game_tgt", "game_paused")
    route = GatewayHttpRoute(controls, "play", parents=((game["namespace"], "play"),),
                             sides=GATEWAY_SIDES, observe=game["observe"], samples=3,
                             converge_seconds=90)
    return controls, route, ("game_src", "game_tgt", "paused")


SERVICE_ROUTE = pytest.mark.skipif(
    FLOWS_DROPPED, reason="staging refuses a Service route on this dataplane; "
                          "the gateway variants carry the matches here")


@pytest.fixture(params=[
    pytest.param(("offline-copy", "service"), id="offline-copy-service", marks=SERVICE_ROUTE),
    pytest.param(("snapshot-cdc", "service"), id="snapshot-cdc-service", marks=SERVICE_ROUTE),
    pytest.param(("offline-copy", "gateway"), id="offline-copy-gateway"),
    pytest.param(("snapshot-cdc", "gateway"), id="snapshot-cdc-gateway")])
def application(request, postgres_database, now, volume_business, other_tenant, tmp_path):
    """On snapshot-cdc the board's files reach the target while it serves, and
    the stop carries only what changed since."""
    strategy, entry = request.param
    game = request.getfixturevalue(
        "session_business" if entry == "service" else "gateway_session_business")
    h = prepared(postgres_database, now, tmp_path, board_contract(), engine="kubernetes-volume",
                 strategy=strategy)
    board = VolumeWorld(volume_business)
    board_part = board_settings(board, volume_business["controls"](), other_tenant)
    game_controls, game_route, game_routes = game_entry(entry, game)
    game_part = {"source": SessionSide(game_controls, ("deployment", "game-source")),
                 "target": SessionSide(game_controls, ("deployment", "game-target")),
                 "route": game_route, "routes": game_routes,
                 "port": 8080, "image": PYTHON, "run_as": (UID, UID), "drain_seconds": 120,
                 "zone_of": lambda side: "approved-zone-a"}
    board_factory = volume_factory("board-volume", lambda t, a: board_part)

    def factory(tenant_id, application_id, revision):
        # The matches drain first while the board still serves; then the board stops.
        return composite_execution(h["adapter_id"], [
            ("game", session_execution("game-sessions", game_part)),
            ("board", board_factory(tenant_id, application_id, revision))], data="board")

    h["worker"] = worker_for(h, factory, owner="worker-application")
    return {**h, "board": board, "game": game, "board_route": board_part["route"],
            "game_route": game_route}


def test_the_whole_application_is_handed_off_without_cutting_a_match(application):
    h, board, game = application, application["board"], application["game"]
    board.business_write(2)
    before = board.posts("source")
    game["clients"]("players", [{"id": "match", "seconds": 15}])
    game["started"]("players")
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    match = game["results"]("players")["match"]
    assert match["completed"] and match["pod"].startswith("game-source"), match
    assert board.posts("target") == before
    assert h["board_route"].get() == "board_tgt" and h["game_route"].get() == "game_tgt"
    game["clients"]("after", [{"id": "next", "seconds": 2}])
    after = game["results"]("after")["next"]
    assert after["completed"] and after["pod"].startswith("game-target"), after
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    fence = seen["fence.source_writes_denied"]["components"]
    assert fence["game"]["passed"] and fence["board"]["passed"]
    if h["adapter_id"].endswith("snapshot-cdc"):  # the drill needs no stop (ADR-0003)
        order = list(seen)
        assert order.index("security.tested_restore") < order.index(
            "fence.source_writes_denied")
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith("availability.")}))
