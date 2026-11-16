"""A board's files handed off from one cluster to another, with nothing but the
two registered API servers between them: the source reader agent runs in the
first cluster, the target writer agent in the second, and the bytes pass
through the worker. The application then starts in the second cluster on its
own database and uploads. ``test-env.sh second`` creates the second cluster
(``SOVA_TEST_KUBE_CONTEXT_B``)."""

from datetime import UTC, datetime
import json
import os
import time

from adapter_conformance import apply, command
import pytest
from volume_business import PYTHON, RULES, UID, writer
from volume_world import VolumeWorld

from sova.adapters.kubernetes.volume_agents import VolumeAgents
from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls
from sova.adapters.kubernetes_volume_handoff import KubernetesVolumeHandoffAdapter, VolumeSide
from sova.adapters.kubernetes_volume_probes import KubernetesVolumeProbes
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kubernetes
HANDOFF = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target", "commit_route")


class Route:
    """The entry point between two clusters (a DNS name, a load balancer)."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.fixture
def second_cluster():
    from kind_support import delete_namespace, kind_namespace, kubectl

    context = os.environ.get("SOVA_TEST_KUBE_CONTEXT_B")
    if not context:
        pytest.skip("The second cluster is not up: run infra/test-env/test-env.sh second")
    context, namespace, controls = kind_namespace(
        RULES, [writer("board-target", "board", "target", replicas=0, role="app"),
                writer("board-worker-target", "board-worker", "target", replicas=0,
                       role="worker")],
        prefix="volume-b", controls_class=KubernetesVolumeControls, context=context)

    def run(*args):
        return kubectl(context, "-n", namespace, *args)

    try:
        yield {"context": context, "namespace": namespace, "controls": controls, "run": run}
    finally:
        delete_namespace(context, namespace)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and namespace in kubectl(
                context, "get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"):
            time.sleep(1)


def sides(volume_business, second_cluster):
    a, b = volume_business["controls"](), second_cluster["controls"]()
    return (VolumeSide(a, ("board-data-source",), ("deployment", "board-source"),
                       (("deployment", "board-worker-source"),)),
            VolumeSide(b, ("board-data-target",), ("deployment", "board-target"),
                       (("deployment", "board-worker-target"),)))


def test_files_move_between_two_clusters_through_their_api_servers_only(
        volume_business, second_cluster):
    board = VolumeWorld(volume_business)
    board.business_write(3)
    before = board.posts("source")
    source, target = sides(volume_business, second_cluster)
    route = Route("board_src")
    adapter = KubernetesVolumeHandoffAdapter(
        "kubernetes-volume-handoff", source=source, target=target, route=route,
        routes=("board_src", "board_tgt"), agent_key=VolumeWorld.KEY, image=PYTHON,
        run_as=(UID, UID), storage_class="standard")
    spent = {}
    for action in HANDOFF:
        started = time.monotonic()
        apply(adapter, action)
        spent[action] = round(time.monotonic() - started, 1)
        observation = adapter.readback(command(adapter.adapter_id, action), datetime.now(UTC))
        assert observation.status == EffectStatus.APPLIED, (action, observation.evidence)
    # The application starts in the second cluster on what was copied.
    b = second_cluster
    pod = b["run"]("get", "pods", "-l", "app=board,side=target", "-o",
                   "jsonpath={.items[0].metadata.name}").strip()
    after = json.loads(b["run"]("get", "--raw", f"/api/v1/namespaces/{b['namespace']}/pods/"
                                                f"{pod}:8080/proxy/posts"))
    assert after == before
    assert not board.can_write("source", "app") and not board.can_write("source", "other")
    print("seconds per step:", json.dumps(spent))


def test_the_probes_measure_across_the_two_clusters(volume_business, second_cluster):
    source, target = sides(volume_business, second_cluster)
    adapter = KubernetesVolumeHandoffAdapter(
        "kubernetes-volume-handoff", source=source, target=target, route=Route("board_src"),
        routes=("board_src", "board_tgt"), agent_key=VolumeWorld.KEY, image=PYTHON,
        run_as=(UID, UID), storage_class="standard")
    for action in HANDOFF[:4]:
        apply(adapter, action)
    probes = KubernetesVolumeProbes(
        source=source, target=target,
        agents=VolumeAgents(label="probecross", key=VolumeWorld.KEY, image=PYTHON,
                            run_as=(UID, UID)),
        write_probe=(8080, "/probe"), databases=("board.db",), settle_seconds=1.0)
    for check in (lambda: probes.write_denied("source"), lambda: probes.write_denied("target"),
                  lambda: probes.content_equal("board.db"),
                  lambda: probes.content_equal("uploads"),
                  lambda: probes.restore_drill("sova-restore")):
        passed, observation = check()
        assert passed, observation


@pytest.mark.perf
def test_copy_throughput_between_two_clusters(volume_business, second_cluster):
    """How fast bytes pass the two API servers' proxies (opt-in: SOVA_TEST_PERF=1)."""
    if os.environ.get("SOVA_TEST_PERF") != "1":
        pytest.skip("SOVA_TEST_PERF=1 runs the throughput measurement")
    from volume_business import bare_pod

    size_mb = int(os.environ.get("SOVA_TEST_PERF_VOLUME_MB", "128"))
    run = volume_business["run"]
    run("apply", "-f", "-", stdin=json.dumps(bare_pod(
        "bulk", "board-data-source", ["python3", "-c",
                                      f"import os; f = open('/data/bulk.bin', 'wb'); "
                                      f"[f.write(os.urandom(1 << 20)) for _ in range({size_mb})]"],
        read_only=False)))
    run("wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/bulk", "--timeout=300s")
    run("delete", "pod", "bulk", "--wait=true")
    source, target = sides(volume_business, second_cluster)
    adapter = KubernetesVolumeHandoffAdapter(
        "kubernetes-volume-handoff", source=source, target=target, route=Route("board_src"),
        routes=("board_src", "board_tgt"), agent_key=VolumeWorld.KEY, image=PYTHON,
        run_as=(UID, UID), storage_class="standard")
    for action in HANDOFF[:3]:
        apply(adapter, action)
    started = time.monotonic()
    apply(adapter, "copy_content")
    copied = time.monotonic() - started
    started = time.monotonic()
    observation = adapter.readback(command(adapter.adapter_id, "verify_final_state"),
                                   datetime.now(UTC))
    verified = time.monotonic() - started
    assert observation.status == EffectStatus.APPLIED
    print(f"copy {size_mb} MiB: {copied:.1f}s ({size_mb / copied:.1f} MiB/s), "
          f"verify both sides: {verified:.1f}s")
