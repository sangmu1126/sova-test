"""The volume probes fail where they must, on the real board application:
a live writer, an unregistered Pod with the claim mounted read-write, a
changed database row, a missing upload. A probe that only ever passes would
make every signed obligation meaningless."""

from datetime import UTC, datetime
import json

from adapter_conformance import apply, command
import pytest
from volume_business import PYTHON, UID, bare_pod
from volume_world import VolumeWorld

from sova.adapters.kubernetes.volume_agents import VolumeAgents
from sova.adapters.kubernetes_volume_probes import KubernetesVolumeProbes
from sova.domain.contracts import EffectStatus

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
                            run_as=(UID, UID), fs_group=UID),
        write_probe=(8080, "/probe"), databases=("board.db",),
        invariants={"BOARD-01": {"database": "board.db", "files": FILES, "under": "uploads"},
                    "BOARD-02": {"database": "board.db",
                                 "violations": "SELECT id FROM posts WHERE title = ''"}},
        settle_seconds=1.0)


def on_claim(world, name, claim, script, *, read_only=False):
    """Run ``script`` in a Pod of the test that mounts ``claim``; its output."""
    world.run("apply", "-f", "-", stdin=json.dumps(bare_pod(
        name, claim, ["python3", "-c", script], read_only=read_only)))
    world.run("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{name}",
              "--timeout=120s")
    out = world.run("logs", name)
    world.run("delete", "pod", name, "--wait=true")
    return out


def on_target(world, name, script):
    on_claim(world, name, "board-data-target", script)


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


def test_the_restore_drill_reproduces_what_the_target_holds(world):
    """Before the write stop (snapshot-cdc) the target may hold a copy of a
    database in use, or none yet. The drill asks whether a restore reproduces
    the target, so it passes on what is there and shows it; whether the
    database is intact is the content comparison's question, inside the stop."""
    p = probes(world)
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    passed, drill = p.restore_drill("sova-restore")
    assert passed, drill
    assert drill["databases"]["board.db"] == {"equal": True, "present": True,
                                              "target_integrity": ["ok"],
                                              "restored_integrity": ["ok"]}
    # Torn: SQLite cannot read the first page.
    on_target(world, "tear", "f = open('/data/board.db', 'r+b'); f.seek(16); "
                             "f.write(bytes(range(256)) * 8); f.close()")
    passed, drill = p.restore_drill("sova-restore")
    assert passed, drill
    seen = drill["databases"]["board.db"]
    assert seen["equal"] and seen["target_integrity"] == seen["restored_integrity"] != ["ok"]
    passed, compared = p.content_equal("board.db")  # inside the stop this holds the plan
    assert not passed and compared["target"]["integrity"] != ["ok"]
    # Not there yet.
    on_target(world, "drop-db", "import os; os.unlink('/data/board.db')")
    passed, drill = p.restore_drill("sova-restore")
    assert passed, drill
    assert drill["databases"]["board.db"]["present"] is False


LINKED = ["objects/ab/cdef", "clone/objects/ab/cdef", "pack", "Maildir/tmp/1.msg",
          "Maildir/new/1.msg"]
MAKE_LINKS = """
import os
os.makedirs('/data/objects/ab'); os.makedirs('/data/clone/objects/ab')
open('/data/objects/ab/cdef', 'wb').write(os.urandom(300000))
os.link('/data/objects/ab/cdef', '/data/clone/objects/ab/cdef')
os.link('/data/objects/ab/cdef', '/data/pack')
os.makedirs('/data/Maildir/tmp'); os.makedirs('/data/Maildir/new')
open('/data/Maildir/tmp/1.msg', 'w').write('Subject: hi\\n')
os.link('/data/Maildir/tmp/1.msg', '/data/Maildir/new/1.msg')
"""
READ_INODES = (f"import json, os; print(json.dumps({{n: [os.stat('/data/' + n).st_ino, "
               f"os.stat('/data/' + n).st_nlink] for n in {LINKED!r}}}))")


def test_hard_links_reach_the_target_as_links(world):
    """A git object store cloned locally and a Maildir delivery: names that share
    one file on the source share one file on the target - measured by inode from
    a Pod of the test, not by the agent. A link lost on the target behind the
    plan's back is a difference the readback and the probes show, and the next
    copy restores it."""
    p = probes(world)
    on_claim(world, "make-links", "board-data-source", MAKE_LINKS)
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    seen = world.adapter().readback(command("kubernetes-volume-handoff", "copy_content"),
                                    datetime.now(UTC))
    assert seen.status == EffectStatus.APPLIED, seen.evidence
    claim = seen.evidence["claims"][0]
    assert claim["source"]["link_groups"] == claim["target"]["link_groups"] == 2
    found = json.loads(on_claim(world, "inodes", "board-data-target", READ_INODES,
                                read_only=True))
    assert len({found[n][0] for n in LINKED[:3]}) == 1 and found["pack"][1] == 3, found
    assert len({found[n][0] for n in LINKED[3:]}) == 1 and found["Maildir/new/1.msg"][1] == 2
    for check in (lambda: p.content_equal("objects"), lambda: p.content_equal("Maildir"),
                  lambda: p.restore_drill("sova-restore")):
        passed, observation = check()
        assert passed, observation
    # The same bytes and mode under the name, but a file of its own.
    on_target(world, "unlink-pack", "import os, shutil; mode = os.stat('/data/pack').st_mode; "
                                    "shutil.copyfile('/data/pack', '/data/pack.new'); "
                                    "os.chmod('/data/pack.new', mode & 0o7777); "
                                    "os.replace('/data/pack.new', '/data/pack')")
    seen = world.adapter().readback(command("kubernetes-volume-handoff", "copy_content"),
                                    datetime.now(UTC))
    assert seen.status == EffectStatus.UNKNOWN
    store, rest = LINKED[:3], ["clone/objects/ab/cdef", "objects/ab/cdef"]
    assert seen.evidence["claims"][0]["differences"] == [  # every name's group changed
        {"path": path, "source_links": sorted(store),
         "target_links": ["pack"] if path == "pack" else rest} for path in sorted(store)]
    passed, pack = p.content_equal("pack")
    assert not passed and pack["source"]["link_groups"] == 1 and \
        pack["target"]["link_groups"] == 0
    apply(world.adapter(), "copy_content")
    seen = world.adapter().readback(command("kubernetes-volume-handoff", "copy_content"),
                                    datetime.now(UTC))
    assert seen.status == EffectStatus.APPLIED, seen.evidence
