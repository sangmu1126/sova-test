"""The volume probes fail where they must, on the real board application:
a live writer, an unregistered Pod with the claim mounted read-write, a
changed database row, a missing upload. A probe that only ever passes would
make every signed obligation meaningless."""

import json

from adapter_conformance import apply
import pytest
from volume_business import PYTHON, UID, bare_pod
from volume_world import VolumeWorld

from sova.adapters.kubernetes.volume_agents import VolumeAgents
from sova.adapters.kubernetes_volume_probes import KubernetesVolumeProbes

pytestmark = pytest.mark.kubernetes

FILES = "SELECT attachment FROM posts WHERE attachment IS NOT NULL"


@pytest.fixture
def world(volume_business):
    return VolumeWorld(volume_business)


def probes(world):
    controls = world.b["controls"]()
    return KubernetesVolumeProbes(
        source=world._side("source", controls), target=world._side("target", controls),
        agents=VolumeAgents(label="probetest", key=VolumeWorld.KEY, image=PYTHON,
                            run_as=(UID, UID)),
        write_probe=(8080, "/probe"), databases=("board.db",),
        invariants={"BOARD-01": {"database": "board.db", "files": FILES, "under": "uploads"},
                    "BOARD-02": {"database": "board.db",
                                 "violations": "SELECT id FROM posts WHERE title = ''"}},
        settle_seconds=1.0)


def on_target(world, name, script):
    world.run("apply", "-f", "-", stdin=json.dumps(bare_pod(
        name, "board-data-target", ["python3", "-c", script], read_only=False)))
    world.run("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{name}",
              "--timeout=120s")
    world.run("delete", "pod", name, "--wait=true")


def test_each_probe_fails_on_what_it_measures(world):
    p = probes(world)
    passed, live = p.write_denied("source")
    assert not passed and "write_accepted" in live["outcomes"]["deployment/board-source"]
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    passed, fenced = p.write_denied("source")
    assert passed, fenced
    assert fenced["unchanged"] is True
    for check in (lambda: p.content_equal("board.db"), lambda: p.content_equal("uploads"),
                  lambda: p.invariant("BOARD-01"), lambda: p.invariant("BOARD-02")):
        passed, observation = check()
        assert passed, observation
    # A row changed and an upload removed on the target, behind the plan's back.
    on_target(world, "edit-row", "import sqlite3; c = sqlite3.connect('/data/board.db'); "
                                 "c.execute(\"UPDATE posts SET title='changed' WHERE id=3\"); "
                                 "c.commit(); c.close()")
    on_target(world, "drop-file", "import os; os.unlink('/data/uploads/5.txt')")
    passed, row = p.content_equal("board.db")
    assert not passed and row["source"]["tables"]["posts"] != row["target"]["tables"]["posts"]
    passed, uploads = p.content_equal("uploads")
    assert not passed and uploads["source"]["entries"] == uploads["target"]["entries"] + 1
    passed, invariant = p.invariant("BOARD-01")
    assert not passed and invariant["missing"] == ["5.txt"]
    passed, unknown = p.invariant("BOARD-99")
    assert not passed and "no probe registered" in unknown["reason"]


def test_a_pod_nobody_registered_keeps_writes_possible(world):
    p = probes(world)
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    assert p.write_denied("source")[0]
    world.open_pending_write()  # a Pod started by hand with the claim read-write
    passed, observation = p.write_denied("source")
    assert not passed and observation["unregistered_writers"] == ["maintenance"]
    world.resolve_pending_write()
    assert p.write_denied("source")[0]


def test_transport_is_reported_as_not_verified(world):
    passed, observation = probes(world).transport_encrypted("target")
    assert not passed and observation["encrypted"] is False
