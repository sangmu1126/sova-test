"""Where an agent for a single-node (ReadWriteOnce) volume runs: beside the Pod
that holds it, named by that Pod's labels (a Pod affinity, which GKE
Autopilot admits; a node affinity on the host name it refuses)."""

from types import SimpleNamespace

from sova.adapters.kubernetes.volume_agents import VolumeAgents, beside

IMAGE = "docker.io/library/python@sha256:" + "a" * 64


def holder(name, node, labels=None, claim="data", phase="Running"):
    return {"metadata": {"name": name, **({"labels": labels} if labels else {})},
            "spec": {"nodeName": node, "volumes": [
                {"name": "v", "persistentVolumeClaim": {"claimName": claim}}],
                "containers": [{"name": "c", "volumeMounts": [{"name": "v", "mountPath": "/d"}]}]},
            "status": {"phase": phase}}


def controls(*pods):
    return SimpleNamespace(all_pods=lambda: list(pods),
                           registration=SimpleNamespace(namespace="apps"))


def test_an_agent_goes_beside_the_pod_holding_the_claim_by_its_labels():
    labels = {"app": "board", "side": "source", "pod-template-hash": "abc"}
    c = controls(holder("board-1", "node-a", labels), holder("other", "node-b", {"app": "x"},
                                                              claim="elsewhere"))
    place = beside(c, ("data",))
    assert place == {"labels": labels}
    pod = VolumeAgents(label="unittest", key=b"k" * 32, image=IMAGE, run_as=(1, 1)).pod(
        c, ("data",), "agent", False, place)
    term = pod["spec"]["affinity"]["podAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert term == {"labelSelector": {"matchLabels": labels},
                    "topologyKey": "kubernetes.io/hostname"}
    assert "nodeAffinity" not in pod["spec"]["affinity"]


def test_no_holder_or_holders_on_two_nodes_place_nothing():
    assert beside(controls(), ("data",)) is None
    assert beside(controls(holder("a", "n1", {"x": "1"}), holder("b", "n2", {"x": "2"})),
                  ("data",)) is None
    assert beside(controls(holder("a", None, {"x": "1"})), ("data",)) is None  # not placed


def test_a_holder_without_labels_falls_back_to_its_node():
    place = beside(controls(holder("bare", "node-a")), ("data",))
    assert place == {"node": "node-a"}
    pod = VolumeAgents(label="unittest", key=b"k" * 32, image=IMAGE, run_as=(1, 1)).pod(
        controls(), ("data",), "agent", False, place)
    assert pod["spec"]["affinity"]["nodeAffinity"]
