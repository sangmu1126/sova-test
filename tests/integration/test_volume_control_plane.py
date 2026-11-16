"""A file-state application's volume handed off by the whole control plane:
registered, compiled, approved and admitted through the service, then run by
a worker - the volume adapter through the effect ledger, every obligation
measured by the volume probes and signed, the authority advanced only by the
coordinator. The application is the SQLite-and-uploads board of
``volume_business``; another tenant's volume lives in a namespace of its own."""

import json

from control_plane_support import board_contract, prepared, signed_results, worker_for
import pytest
from volume_business import PYTHON, UID
from volume_world import Route, VolumeWorld

from sova.domain.handoff_authority import HandoffState
from sova.service.volume_execution import volume_factory

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes]
FILES = "SELECT attachment FROM posts WHERE attachment IS NOT NULL"


def board_settings(board, controls, other):
    """What the execution file says about the board's volume unit."""
    route = Route(controls, "board", {"board_src": {"app": "board", "side": "source"},
                                      "board_tgt": {"app": "board", "side": "target"}},
                  converge_seconds=60)
    return {"source": board._side("source", controls), "target": board._side("target", controls),
            "route": route, "routes": ("board_src", "board_tgt"), "agent_key": VolumeWorld.KEY,
            "agent_image": PYTHON, "run_as": (UID, UID), "storage_class": "standard",
            "write_probe": (8080, "/probe"), "databases": ("board.db",),
            "invariants": {"BOARD-01": {"database": "board.db", "under": "uploads",
                                        "files": FILES}},
            "zone_of": lambda side: "approved-zone-a",  # the operator's inventory
            "other": (other, "records"), "scratch_claim": "sova-restore"}


@pytest.fixture
def volume_handoff(postgres_database, now, volume_business, other_tenant, tmp_path):
    h = prepared(postgres_database, now, tmp_path, board_contract(), engine="kubernetes-volume")
    board = VolumeWorld(volume_business)
    settings = board_settings(board, volume_business["controls"](), other_tenant)
    h["worker"] = worker_for(h, volume_factory(h["adapter_id"], lambda t, a: settings),
                             owner="worker-volume")
    return {**h, "board": board, "route": settings["route"]}


def test_a_volume_unit_is_handed_off_by_the_control_plane(volume_handoff):
    h, board = volume_handoff, volume_handoff["board"]
    assert h["adapter_id"] == "volume-offline-copy"
    board.business_write(2)
    before = board.posts("source")
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == "board_tgt"
    assert board.posts("target") == before  # rows, integrity and every upload
    assert not board.can_write("source", "app") and not board.can_write("source", "other")
    assert board.can_write("target", "app") and not board.can_write("target", "other")
    # Every obligation was measured by a probe and signed, none failed.
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    # Nothing of the probes or the adapter is left behind.
    pods = board._pods()
    assert not [p["metadata"]["name"] for p in pods
                if p["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "sova"]
    claims = json.loads(board.run("get", "pvc", "-o", "json"))["items"]
    assert not [c for c in claims if c["metadata"]["name"].startswith("sova-restore")
                and not c["metadata"].get("deletionTimestamp")]
    seen = h["observations"]
    assert seen["data.board.db"]["method"] == "sqlite_logical"
    assert seen["business.BOARD-01"]["missing"] == [] and seen["business.BOARD-01"]["referenced"]
    assert seen["fence.source_writes_denied"]["unchanged"] is True
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith(("availability.", "operations."))}))
