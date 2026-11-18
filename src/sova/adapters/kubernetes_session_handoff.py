"""Reference adapter: a server that holds live sessions in memory (§18.2).

A WebSocket game server, a chat server, a streaming endpoint: what it holds is
open connections and the state behind them, never anything durable. Under a
single authority (PLANNED_SINGLE_AUTHORITY) the source and the target never
serve at the same time, so the sessions are drained, not moved:

* Pausing admission takes the source Pods out of the route and then waits for
  the sessions already open to end. Behind a Service the selector moves to a
  side that has no Pods (a new connection is refused at once; connections
  already established keep flowing on the kernel's connection tracking -
  where the cluster's Service dataplane keeps them: GKE Dataplane V2 drops
  them, measured. So staging a unit behind a Service measures it first, with
  a connection of its own (``flow_check``), and refuses the unit if open
  connections do not survive: route it through a Gateway there).
  Behind a Gateway API route the rule is left without a backend (the Gateway
  answers new requests with an error; the proxy keeps the upstream of streams
  already open). Either way, what a drain needs.
* Open sessions are counted by the kernel, not by the application: an
  ephemeral container is attached to every live source Pod (it shares the
  Pod's network namespace, nothing else) and writes, every second, the TCP
  connections established on the serving port. Connections from the Pod's
  own node (the kubelet's probes) are not sessions. A count older than the
  freshness bound, or a Pod without its observer, reads back as not known.
* Draining is bounded by ``drain_seconds`` from the moment admission was
  paused (an annotation on the source workload, so any worker instance knows
  it). Past it, ``on_timeout="hold"`` keeps the step unconfirmed until the
  sessions end or an operator decides; ``"terminate"`` lets the handoff go on
  and reports how many sessions the fence will cut (their clients reconnect,
  through the new route, to the target).
* The fence stops the source workload and waits until none of its Pods is
  left; activation starts the target workload and waits until it is ready;
  the route moves the Service to the target's Pods.
* There is no durable state: copying and verifying have nothing to do and
  say so in their evidence. A unit that also has files or a database names
  those in other adapters of the same unit.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import time

from sova.adapters.evidence_bounds import bounded
from sova.adapters.kubernetes.flow_check import ServiceFlowCheck
from sova.adapters.kubernetes.handoff_controls import KubernetesServiceRoute
from sova.adapters.kubernetes.volume_agents import _IMAGE
from sova.adapters.kubernetes.volume_controls import (
    KubernetesVolumeControls,
    alive,
    ready,
    workload_ref,
)
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
NOTHING_DURABLE = frozenset({"start_sync", "copy_content", "apply_final_changes",
                             "verify_final_state"})
OBSERVER = "sova-sessions"
ROUTE_FLOWS = "sova.dev/route-flows-measured"
FLOWS_FRESH_SECONDS = 3600.0  # a measurement older than this is taken again
PAUSED_AT = "sova.dev/admission-paused-at"
FRESH_SECONDS = 15.0

OBSERVER_SOURCE = r'''
import json, socket, struct, sys, time
PORT, EXCLUDE = int(sys.argv[1]), set(sys.argv[2:])
def v4(h):
    return socket.inet_ntop(socket.AF_INET, struct.pack("<I", int(h, 16)))
def v6(h):
    b = bytes.fromhex(h)
    return socket.inet_ntop(socket.AF_INET6, b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4)))
def peers():
    out = []
    for table, convert in (("/proc/net/tcp", v4), ("/proc/net/tcp6", v6)):
        try:
            with open(table) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            local, remote, state = fields[1], fields[2], fields[3]
            if state != "01" or int(local.rsplit(":", 1)[1], 16) != PORT:
                continue
            address = convert(remote.rsplit(":", 1)[0])
            address = address[7:] if address.startswith("::ffff:") else address
            if address not in EXCLUDE:
                out.append(f"{address}:{int(remote.rsplit(':', 1)[1], 16)}")
    return sorted(out)
while True:
    found = peers()
    print(json.dumps({"at": time.time(), "established": len(found), "peers": found[:20]}),
          flush=True)
    time.sleep(1)
'''


@dataclass(frozen=True)
class SessionSide:
    """One side: its registered controls and the workload serving the sessions."""

    controls: KubernetesVolumeControls
    app: tuple

    def __post_init__(self):
        if not isinstance(self.controls, KubernetesVolumeControls):
            raise ContractError("Registered workload controls required")
        if workload_ref(self.app)[0] == "cronjob":
            raise ContractError("A session server runs as a Deployment or StatefulSet")


class KubernetesSessionHandoffAdapter:
    def __init__(self, adapter_id, *, source, target, route, routes, port, image, run_as,
                 drain_seconds=300.0, on_timeout="hold", app_replicas=1, settle_seconds=60.0,
                 start_seconds=120.0):
        """``route`` is the Service route store; ``routes`` its values for
        (source, target, paused): the paused side's selector matches no Pod."""
        if not isinstance(source, SessionSide) or not isinstance(target, SessionSide):
            raise ContractError("A source and a target session side required")
        if not all(callable(getattr(route, n, None))
                   for n in ("get", "set", "pause", "paused", "left")) or \
                type(routes) is not tuple or len(set(routes)) != 3:
            raise ContractError("A route store that can pause, and its source, target and "
                                "paused values, required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ContractError("The serving port required")
        if type(image) is not str or not _IMAGE.fullmatch(image):
            raise ContractError("An observer image pinned by digest required")
        if type(run_as) is not tuple or len(run_as) != 2 or not all(
                type(v) is int and 1 <= v < 2**31 for v in run_as):
            raise ContractError("A non-root user and group for the observer required")
        if on_timeout not in ("hold", "terminate"):
            raise ContractError("on_timeout is hold or terminate")
        if type(app_replicas) is not int or not 1 <= app_replicas <= 100:
            raise ContractError("Bounded application replicas required")
        self.adapter_id, self.route, self.routes = adapter_id, route, routes
        self.sides = {"source": source, "target": target}
        self.port, self.image, self.run_as = port, image, run_as
        self.drain_seconds, self.on_timeout = float(drain_seconds), on_timeout
        self.app_replicas = app_replicas
        self.settle_seconds, self.start_seconds = settle_seconds, start_seconds
        # A pause behind a Service relies on the cluster's dataplane: measured.
        self.measure_flows = isinstance(route, KubernetesServiceRoute)

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported handoff action for this adapter")

    def apply(self, command):
        if command.action not in NOTHING_DURABLE:
            getattr(self, "_" + command.action)()

    def readback(self, command, now):
        if command.action in NOTHING_DURABLE:
            holds, detail = True, {"durable_state": "none"}
        else:
            holds, detail = getattr(self, "_check_" + command.action)()
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **bounded(detail)}, limit=8192))

    # -- observation -------------------------------------------------------------
    @staticmethod
    def _wait(predicate, seconds, step=0.5):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)
        return True

    def _workload(self, side):
        s = self.sides[side]
        found = s.controls.workload(*s.app)
        if found is None:
            raise ContractError(f"{side} session server {s.app[0]}/{s.app[1]} does not exist")
        selector = (found.get("spec") or {}).get("selector") or {}
        if set(selector) - {"matchLabels"} or not selector.get("matchLabels"):
            raise ContractError("Only equality label selectors are decided")
        return found, selector["matchLabels"]

    def _pods(self, side):
        found, selector = self._workload(side)
        pods = [p for p in self.sides[side].controls.all_pods(selector) if alive(p)]
        return found, pods

    def _stopped(self, side):
        found, pods = self._pods(side)
        wanted = (found.get("spec") or {}).get("replicas")
        return wanted == 0 and not pods, {
            "server": "/".join(self.sides[side].app), "desired": wanted,
            "pods_alive": sorted(p["metadata"]["name"] for p in pods)[:20]}

    def _scale(self, side, replicas):
        s = self.sides[side]
        found, _ = self._workload(side)
        if (found.get("spec") or {}).get("replicas") != replicas:
            s.controls.set_replicas(s.app[0], s.app[1], found["metadata"]["resourceVersion"],
                                    replicas)

    def _out_of_route(self):
        return self.route.paused(self.routes[2])

    def _paused_at(self):
        found, _ = self._workload("source")
        stamp = ((found.get("metadata") or {}).get("annotations") or {}).get(PAUSED_AT)
        try:
            return datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            return None

    def _observer(self, pod):
        security = {"runAsNonRoot": True, "runAsUser": self.run_as[0],
                    "runAsGroup": self.run_as[1], "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"}}
        node = (pod.get("status") or {}).get("hostIP")
        return {"name": OBSERVER, "image": self.image, "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "-I", "-c", OBSERVER_SOURCE, str(self.port),
                            *([node] if node else [])],
                "securityContext": security}

    def _sessions(self, pod):
        """(established count or None, detail) from the Pod's observer."""
        name = pod["metadata"]["name"]
        statuses = {c["name"]: c for c in (pod.get("status") or {}).get(
            "ephemeralContainerStatuses") or ()}
        if "running" not in (statuses.get(OBSERVER, {}).get("state") or {}):
            return None, {"pod": name, "observer": "not_running"}
        try:
            line = self.sides["source"].controls.container_log(name, OBSERVER).strip()
            seen = json.loads(line.splitlines()[-1])
        except (ValueError, IndexError, ContractError):
            return None, {"pod": name, "observer": "no_reading"}
        age = time.time() - float(seen["at"])
        if age > FRESH_SECONDS:
            return None, {"pod": name, "observer": "stale", "age_seconds": round(age, 1)}
        return seen["established"], {"pod": name, "established": seen["established"],
                                     "peers": seen["peers"][:5]}

    def _flows(self):
        """The recorded measurement of the Service dataplane, or None."""
        found, _ = self._workload("target")
        note = ((found.get("metadata") or {}).get("annotations") or {}).get(ROUTE_FLOWS)
        try:
            record = json.loads(note) if note else None
            age = (datetime.now(UTC) - datetime.fromisoformat(record["at"])).total_seconds()
        except (ValueError, KeyError, TypeError):
            return None
        return {**record, "age_seconds": round(age)}

    def _measure_flows(self):
        label = hashlib.sha256(self.adapter_id.encode()).hexdigest()[:8]
        survive, detail = ServiceFlowCheck(self.sides["source"].controls, image=self.image,
                                           run_as=self.run_as, label=label,
                                           start_seconds=self.start_seconds).measure()
        record = {"at": datetime.now(UTC).isoformat(), "survive": survive, **detail}
        found, _ = self._workload("target")
        self.sides["target"].controls.annotate_workload(
            *self.sides["target"].app, found["metadata"]["resourceVersion"], ROUTE_FLOWS,
            json.dumps(record, separators=(",", ":")))
        return record

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        self._scale("target", 0)
        self._wait(lambda: self._stopped("target")[0], self.settle_seconds)
        if self.measure_flows:
            record = self._flows()
            if record is None or record["age_seconds"] > FLOWS_FRESH_SECONDS:
                record = self._measure_flows()
            if record.get("survive") is not True:
                raise ContractError(
                    "On this cluster a connection established through a Service does not "
                    "survive its selector moving (measured): pausing this unit behind a "
                    "Service would cut its open sessions. Route it through a Gateway.")

    def _check_stage_target(self):
        stopped, detail = self._stopped("target")
        if not self.measure_flows:
            return stopped, detail
        record = self._flows()
        fresh = record is not None and record["age_seconds"] <= FLOWS_FRESH_SECONDS
        return stopped and fresh and record.get("survive") is True, {
            **detail, "route_flows": record}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        return self._stopped("target")

    def _pause_admission(self):
        self.route.pause(self.routes[2])
        self._wait(lambda: self._out_of_route()[0], self.settle_seconds, 0.25)
        if self._paused_at() is None:
            found, _ = self._workload("source")
            self.sides["source"].controls.annotate_workload(
                *self.sides["source"].app, found["metadata"]["resourceVersion"], PAUSED_AT,
                datetime.now(UTC).isoformat())
        for pod in self._pods("source")[1]:
            existing = [c["name"] for c in (pod.get("spec") or {}).get(
                "ephemeralContainers") or ()]
            if OBSERVER not in existing:
                self.sides["source"].controls.add_ephemeral_container(
                    pod["metadata"]["name"], pod["metadata"]["resourceVersion"],
                    self._observer(pod), first=not existing)
        paused_at = self._paused_at()
        left = self.drain_seconds - (datetime.now(UTC) - paused_at).total_seconds()
        # Wait for the open sessions to end, but never past the drain bound.
        self._wait(lambda: self._drained()[0], max(left, 0) + 2, 1.0)

    def _drained(self):
        _, pods = self._pods("source")
        counts = [self._sessions(p) for p in pods]
        return all(n == 0 for n, _ in counts), counts

    def _check_pause_admission(self):
        out, seen = self._out_of_route()
        paused_at = self._paused_at()
        drained, counts = self._drained()
        known = all(n is not None for n, _ in counts)
        passed = paused_at is not None and (
            datetime.now(UTC) - paused_at).total_seconds() >= self.drain_seconds
        terminating = sum(n or 0 for n, _ in counts)
        holds = out and paused_at is not None and known and (
            drained or (passed and self.on_timeout == "terminate"))
        return holds, {"out_of_route": out, "route": seen,
                       "paused_at": paused_at.isoformat() if paused_at else None,
                       "drain_seconds": self.drain_seconds, "deadline_passed": passed,
                       "on_timeout": self.on_timeout, "sessions": [d for _, d in counts][:20],
                       "sessions_to_terminate": terminating if holds and not drained else 0}

    def _fence_source_writes(self):
        self._scale("source", 0)
        self._wait(lambda: self._stopped("source")[0], self.settle_seconds)

    def _check_fence_source_writes(self):
        out, _ = self._out_of_route()
        stopped, detail = self._stopped("source")
        # Once the route has moved on, the source being stopped is what still holds.
        moved = self.route.left(self.routes[0])
        return stopped and (out or moved), {"source": detail, "out_of_route": out or moved}

    def _activate_target(self):
        self._scale("target", self.app_replicas)
        self._wait(lambda: self._check_activate_target()[0], self.start_seconds)

    def _check_activate_target(self):
        found, pods = self._pods("target")
        serving = sorted(p["metadata"]["name"] for p in pods if ready(p))
        source, detail = self._stopped("source")
        wanted = (found.get("spec") or {}).get("replicas")
        return wanted == self.app_replicas and bool(serving) and source, {
            "desired": wanted, "serving_pods": serving[:20], "source": detail}

    def _commit_route(self):
        self.route.set(self.routes[1])

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.routes[1], {"route": current}
