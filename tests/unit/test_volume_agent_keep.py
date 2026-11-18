"""Readers kept between the steps of one execution: reused only by the object
that started them, never a writer, never on a claim that admits one Pod;
reaped when a use fails, when the execution ends, and - every agent on the
target claims, whoever started it - before the application takes the volume.
Against a stand-in cluster that records what is created and deleted."""

import copy
from types import SimpleNamespace

import pytest

from sova.adapters.kubernetes.volume_agents import VolumeAgents, remove_agents
from sova.domain.contracts import ContractError

IMAGE = "docker.io/library/python@sha256:" + "a" * 64


class Cluster:
    def __init__(self, access=("ReadWriteOnce",)):
        self.registration = SimpleNamespace(endpoint="https://api.test:6443", namespace="apps")
        self.access, self.live, self.created, self.deleted = access, {}, [], []

    def claim(self, name):
        return {"spec": {"accessModes": list(self.access)}}

    def create_pod(self, body):
        body = copy.deepcopy(body)
        body["spec"]["nodeName"] = "node-a"
        body["status"] = {"phase": "Running", "podIP": "10.0.0.9",
                          "conditions": [{"type": "Ready", "status": "True"}],
                          "containerStatuses": [{"state": {"running": {
                              "startedAt": "2026-11-19T00:00:00Z"}}}]}
        self.live[body["metadata"]["name"]] = body
        self.created.append(body["metadata"]["name"])

    def pod(self, name):
        return self.live.get(name)

    def delete_pod(self, name):
        self.live.pop(name, None)
        self.deleted.append(name)

    def all_pods(self, selector=None):
        return [p for p in self.live.values() if all(
            (p["metadata"].get("labels") or {}).get(k) == v for k, v in (selector or {}).items())]

    def pods(self, selector):
        return {p["metadata"]["name"] for p in self.all_pods(selector)}


def agents(label="unittest", **options):
    return VolumeAgents(label=label, key=b"k" * 32, image=IMAGE, run_as=(10001, 10001),
                        start_seconds=1.0, **options)


def use(a, cluster, claims=("data",), writable=False):
    with a.running((cluster, claims, writable)) as (agent,):
        return agent.name


def test_a_reader_is_kept_for_the_next_use_and_reaped_at_the_end():
    c, a = Cluster(), agents(keep=True)
    first, second = use(a, c), use(a, c)
    assert first == second and c.created == [first] and c.deleted == []
    assert use(a, c, ("other",)) != first  # other claims, another agent
    a.release()
    assert c.live == {} and len(c.deleted) == 2


def test_a_writer_is_never_kept():
    c, a = Cluster(), agents(keep=True)
    first = use(a, c, writable=True)
    assert c.live == {} and use(a, c, writable=True) != first


def test_without_keep_every_agent_is_reaped_after_its_use():
    c, a = Cluster(), agents()
    first = use(a, c)
    assert c.live == {} and use(a, c) != first


def test_a_use_that_fails_leaves_nothing_for_the_next():
    c, a = Cluster(), agents(keep=True)
    kept = use(a, c)
    with pytest.raises(RuntimeError):
        with a.running((c, ("data",), False)):
            raise RuntimeError("the probe failed")
    assert kept not in c.live  # the kept one it reused is gone with it
    assert use(a, c) != kept


def test_a_kept_agent_not_yet_marked_ready_but_running_is_reused():
    """An agent answers before the kubelet's next probe marks it ready."""
    c, a = Cluster(), agents(keep=True)
    kept = use(a, c)
    c.live[kept]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    assert use(a, c) == kept and c.created == [kept]


def test_a_kept_agent_that_no_longer_serves_is_replaced():
    c, a = Cluster(), agents(keep=True)
    kept = use(a, c)
    c.live[kept]["status"] = {"phase": "Succeeded", "conditions": [],  # ended (idle)
                              "containerStatuses": [{"state": {"terminated": {}}}]}
    again = use(a, c)
    assert again != kept and kept in c.deleted
    del c.live[again]  # removed by someone else (activation)
    assert use(a, c) not in (kept, again)


def test_a_kept_agent_near_its_idle_end_is_replaced():
    c, a = Cluster(), agents(keep=True, idle_seconds=120)
    kept = use(a, c)
    key = next(iter(a._kept))
    controls, name, used = a._kept[key]
    a._kept[key] = (controls, name, used - 61)  # it would end within the minute
    assert use(a, c) != kept and kept in c.deleted


def test_a_claim_that_admits_one_pod_keeps_no_agent():
    c, a = Cluster(access=("ReadWriteOncePod",)), agents(keep=True)
    first = use(a, c)
    assert c.live == {} and use(a, c) != first


def test_a_start_removes_agents_that_ended_by_themselves():
    c, a = Cluster(), agents()
    other = agents(label="otherunit")
    c.create_pod(a.pod(c, ("data",), "sova-vol-unittest-r-done", False, None))
    c.create_pod(other.pod(c, ("data",), "sova-vol-otherunit-r-done", False, None))
    for name in c.live:
        c.live[name]["status"]["phase"] = "Succeeded"
    use(a, c)
    assert "sova-vol-unittest-r-done" in c.deleted
    assert "sova-vol-otherunit-r-done" in c.live  # another unit's, not this one's


def test_activation_removes_every_agent_on_the_claims_and_nothing_else():
    c = Cluster()
    mine, probes = agents(keep=True), agents(label="probeunit", keep=True)
    on_data = [use(mine, c), use(probes, c)]
    elsewhere = use(mine, c, ("logs",))
    app = {"metadata": {"name": "board-1", "labels": {"app": "board"}},
           "spec": {"volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "data"}}],
                    "containers": [{"name": "c", "volumeMounts": [{"name": "v",
                                                                   "mountPath": "/d"}]}]},
           "status": {"phase": "Running"}}
    c.live["board-1"] = app
    assert sorted(remove_agents(c, ("data",), seconds=1)) == sorted(on_data)
    assert set(c.live) == {elsewhere, "board-1"}
    assert use(mine, c) not in on_data  # its kept one is gone: a new one


def test_every_agent_ends_by_itself_after_its_idle_time():
    pod = agents().pod(Cluster(), ("data",), "a", False, None)
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["SOVA_AGENT_IDLE_SECONDS"] == "600"
    for idle in (60, 5000, "600", True):
        with pytest.raises(ContractError):
            agents(idle_seconds=idle)
    with pytest.raises(ContractError):
        agents(keep="yes")
