"""The Kubernetes volume adapter against the provider-neutral conformance kit,
and what file state adds: an application that must find its own database and
uploads again, ownership the adapter must not paper over, and agent Pods that
must never outlive what they were started for."""

from datetime import UTC, datetime
import json

from adapter_conformance import HandoffAdapterConformance, apply, command, readback
import pytest
from volume_business import bare_pod
from volume_world import VolumeWorld

from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kubernetes

HANDOFF = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target", "commit_route")


@pytest.fixture
def world(volume_business):
    return VolumeWorld(volume_business)


class TestKubernetesVolumeConformance(HandoffAdapterConformance):
    pass


def agents_left(world):
    return [p["metadata"]["name"] for p in world._pods()
            if (p["metadata"].get("labels") or {}).get("app.kubernetes.io/managed-by") == "sova"]


def test_the_application_finds_its_database_and_uploads_on_the_target(world):
    world.business_write(3)
    before = world.posts("source")
    assert before["integrity"] == "ok" and len(before["posts"]) == 23
    for action in HANDOFF:
        apply(world.adapter(), action)
    # Staging and copying describe the time before activation; what must still
    # hold once the application writes on the target is the fence, the
    # activation and the route.
    for action in ("fence_source_writes", "activate_target", "commit_route"):
        assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
    after = world.posts("target")
    assert after == before  # every row, the integrity check and every upload
    # Identifiers continue after the copied ones: none is issued twice.
    world.post(world._serving("target", "board")[0], "/posts?count=1")
    newest = world.posts("target")["posts"][-1][0]
    assert newest == max(p[0] for p in before["posts"]) + 1
    assert agents_left(world) == []


def test_files_owned_by_someone_else_read_back_as_different_not_as_copied(world):
    """The agent never runs as root, so it cannot give a file another owner;
    the comparison says so instead of calling the copy done."""
    world.run("apply", "-f", "-", stdin=json.dumps(bare_pod(
        "other-owner", "board-data-source",
        ["python3", "-c", "open('/data/import.csv','w').write('a,b\\n')"],
        read_only=False, uid=20002)))
    world.run("wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/other-owner",
              "--timeout=120s")
    world.run("delete", "pod", "other-owner", "--wait=true")
    for action in HANDOFF[:4]:
        apply(world.adapter(), action)
    observation = world.adapter().readback(
        command("kubernetes-volume-handoff", "copy_content"), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    differences = observation.evidence["claims"][0]["differences"]
    assert [d["path"] for d in differences] == ["import.csv"]
    assert differences[0]["source"][3] == 20002 and differences[0]["target"][3] != 20002


def test_a_readback_neither_binds_a_claim_nor_leaves_an_agent(world):
    for action in ("copy_content", "verify_final_state", "stage_target"):
        assert readback(world.adapter(), action) == EffectStatus.UNKNOWN
    assert not world._bound("board-data-target")
    assert "board-data-target" not in world.run("get", "pvc", "-o", "name")
    apply(world.adapter(), "stage_target")
    assert world._bound("board-data-target")  # staging binds it, not a readback
    assert readback(world.adapter(), "copy_content") == EffectStatus.UNKNOWN
    assert agents_left(world) == []
