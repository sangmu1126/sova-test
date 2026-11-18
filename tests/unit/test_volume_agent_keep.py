"""Readers kept between the steps of one execution: reused only by the object
that started them, never a writer, never on a claim that admits one Pod;
reaped when a use fails, when the execution ends, and - every agent on the
target claims, whoever started it - before the application takes the volume;
idle kept agents give their room back to an agent that cannot be placed or is
refused by a quota. Against a stand-in cluster that records what is created
and deleted, with a bounded number of Pods it holds."""

import copy
import itertools
from types import SimpleNamespace

import pytest

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.kubernetes.volume_agents import VolumeAgents, make_room, remove_agents
from sova.domain.contracts import ContractError

IMAGE = "docker.io/library/python@sha256:" + "a" * 64
CLUSTERS = itertools.count()
RUNNING = {"phase": "Running", "podIP": "10.0.0.9",
           "conditions": [{"type": "Ready", "status": "True"}],
           "containerStatuses": [{"state": {"running": {"startedAt": "2026-11-19T00:00:00Z"}}}]}
UNSCHEDULABLE = {"phase": "Pending", "conditions": [{
    "type": "PodScheduled", "status": "False", "reason": "Unschedulable",
    "message": "0/1 nodes are available: 1 Too many pods."}]}


class Cluster:
    """``room``: the Pods a node takes - one more stays Pending, unschedulable,
    until another goes. ``quota``: the Pods the namespace admits - one more is
    refused (403)."""

    def __init__(self, access=("ReadWriteOnce",), room=None, quota=None, refusal=403):
        self.registration = SimpleNamespace(endpoint=f"https://api.test-{next(CLUSTERS)}:6443",
                                            namespace="apps")
        self.access, self.live, self.created, self.deleted = access, {}, [], []
        self.room, self.quota, self.refusal, self.refused = room, quota, refusal, 0

    def claim(self, name):
        return {"spec": {"accessModes": list(self.access)}}

    def _placed(self):
        return [n for n, p in self.live.items() if p["status"]["phase"] == "Running"]

    def create_pod(self, body):
        if self.quota is not None and len(self.live) >= self.quota:
            self.refused += 1
            raise KubernetesRequestRefused(self.refusal, "Forbidden")
        body = copy.deepcopy(body)
        body["spec"]["nodeName"] = "node-a"
        full = self.room is not None and len(self._placed()) >= self.room
        body["status"] = copy.deepcopy(UNSCHEDULABLE if full else RUNNING)
        self.live[body["metadata"]["name"]] = body
        self.created.append(body["metadata"]["name"])

    def pod(self, name):
        found = self.live.get(name)
        if found and found["status"]["phase"] == "Pending" and len(self._placed()) < self.room:
            found["status"] = copy.deepcopy(RUNNING)  # the scheduler retries once room frees
        return found

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


def test_an_agent_with_no_room_takes_the_room_idle_kept_agents_hold():
    """A full node (Too many pods): the adapter's and the probes' idle readers
    in that cluster are reaped, the one in use is not, and the new agent runs."""
    c = Cluster(room=3)
    mine, probes = agents(keep=True), agents(label="probeunit", keep=True)
    idle = [use(mine, c, ("logs",)), use(probes, c)]
    with mine.running((c, ("data",), False)) as (busy,):
        assert len(c.live) == 3
        written = use(probes, c, ("data",), writable=True)  # waits for room, then runs
        assert sorted(c.deleted) == sorted([*idle, written])
        assert busy.name in c.live  # in use: never taken
    assert written in c.created and c.live == {busy.name: c.live[busy.name]}
    assert use(mine, c, ("logs",)) not in idle  # a new one where the kept one went


def test_without_kept_agents_an_agent_with_no_room_still_waits_for_its_deadline():
    c = Cluster(room=0)
    with pytest.raises(ContractError, match="in time"):
        use(agents(keep=True), c)


def test_a_quota_refusal_gives_back_the_room_of_idle_kept_agents_once():
    c = Cluster(quota=2)
    mine, probes = agents(keep=True), agents(label="probeunit", keep=True)
    idle = [use(mine, c, ("logs",)), use(probes, c)]
    written = use(mine, c, writable=True)
    assert c.refused == 1 and sorted(c.deleted) == sorted([*idle, written])
    full = Cluster(quota=0)  # nothing kept to give back: refused, not retried
    with pytest.raises(ContractError, match="403"):
        use(agents(keep=True), full)
    assert full.refused == 1


def test_another_refusal_leaves_the_kept_agents_alone():
    c, keeper = Cluster(quota=1, refusal=422), agents(keep=True)
    kept = use(keeper, c)
    with pytest.raises(ContractError, match="422"):
        use(agents(label="probeunit", keep=True), c, ("logs",))
    assert c.refused == 1 and kept in c.live


def test_room_is_made_only_in_the_cluster_that_needs_it():
    here, there = Cluster(), Cluster()
    mine = agents(keep=True)
    near, far = use(mine, here), use(mine, there)
    assert make_room(here) == [near]
    assert near not in here.live and far in there.live
    assert make_room(here) == []
