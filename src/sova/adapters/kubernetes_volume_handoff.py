"""Reference adapter: an application's file state on Kubernetes volumes (§16.1, §16.5).

The unit is one or more PersistentVolumeClaims of an application (a SQLite
database, an upload folder) moved to claims of the same application on the
target side: another namespace, cluster or provider. The same actions and
postconditions as the database adapters, read back from the clusters. What
differs is that a file system has no access list to revoke: whoever mounts a
volume read-write can write it. So the writers *are* the workloads that mount
it, and every postcondition about writing is a statement about Pods:

* Pausing admission stops the application writer (its Deployment or
  StatefulSet scaled to zero, a CronJob suspended) and waits, bounded, until
  no Pod of it is left. For a file-state application this is its downtime:
  there is no read-only mode to fall back to.
* The fence stops every registered writer the same way, and reads back as
  done only when no Pod at all mounts a source claim read-write, registered or
  not, terminating or not. A Pod someone started by hand that still mounts a
  claim keeps the fence unconfirmed until an operator removes it.
* Staging creates the target claims (from the source claims' size and access
  modes, with the target's storage class) and stops every target writer, so
  nothing writes to them before activation. A claim that waits for its first
  consumer is bound by staging, never by a readback.
* Copying runs two short-lived agent Pods (``volume_agent``): one mounting the
  source claims read-only, one mounting the target claims read-write, reached
  through the API server's Pod proxy, so a copy between two clusters needs
  nothing but the two registered API servers. The copy is a difference: only
  entries whose content, kind or mode differ are written, extra ones removed.
  A file reaches its final name only after its content was checked against
  the source's SHA-256; if the source changes during the final copy, the fence
  did not hold and the copy fails.
* Content is compared as a manifest of every entry (path, kind, mode, owner,
  size, SHA-256 or link target). Equal manifests are byte-identical trees.
  Ownership is not changed by the agent (it never runs as root): it writes as
  the registered user and group, the application's own, and a source whose
  files belong to someone else reads back as different, not as copied.
* ``start_sync`` copies while the source still serves (a SQLite file copied
  mid-write may be torn; the final copy after the fence replaces it), so the
  final copy inside the write stop moves only what changed since. It is
  recorded as an annotation on each target claim.
* Activation scales the target application writer to its registered replicas
  and waits, bounded, until a Pod of it is ready. Every other target writer
  stays stopped, and no Pod but the application's mounts a target claim
  read-write.

Agent Pods are reaped after each action or readback (and end by themselves
after an hour at the latest). A readback starts read-only agents only on
claims that are already bound, so it never changes what it reads. Every
action is idempotent; a postcondition that does not hold reads back UNKNOWN.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import re
import secrets
import time

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.kubernetes import volume_agent as _agent_module
from sova.adapters.kubernetes.volume_controls import (
    KubernetesVolumeControls,
    alive,
    matches,
    mounts,
    quantity,
    ready,
    workload_ref,
)
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
AGENT_PORT = 8080
PARTIAL = ".sova-partial-"
SYNCED = "sova.dev/synced-from"
_IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?"
                    r"@sha256:[0-9a-f]{64}|[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")
_WAITING = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                      "CreateContainerConfigError", "CreateContainerError", "CrashLoopBackOff"})

with open(_agent_module.__file__, encoding="utf-8") as _source:
    AGENT_SOURCE = _source.read()
AGENT_DIGEST = hashlib.sha256(AGENT_SOURCE.encode()).hexdigest()


@dataclass(frozen=True)
class VolumeSide:
    """One side: its registered controls, its claims (in the same order on both
    sides) and its writer workloads, the application first."""

    controls: KubernetesVolumeControls
    claims: tuple
    app: tuple
    others: tuple = ()

    def __post_init__(self):
        if not isinstance(self.controls, KubernetesVolumeControls):
            raise ContractError("Registered volume controls required")
        if type(self.claims) is not tuple or not self.claims or \
                len(set(self.claims)) != len(self.claims):
            raise ContractError("Distinct claims required")
        workload_ref(self.app)
        for other in self.others:
            workload_ref(other)
        if self.app in self.others:
            raise ContractError("The application writer is registered once")

    @property
    def namespace(self):
        return self.controls.registration.namespace

    @property
    def writers(self):
        return (self.app, *self.others)


class _Agent:
    """Requests to one agent Pod through the API server."""

    def __init__(self, controls, name, token):
        self.controls, self.name, self.token = controls, name, token

    def _call(self, method, path, query, *, body=None, timeout=120, limit=1 << 20):
        return self.controls.proxy(method, self.name, AGENT_PORT, path, query,
                                   headers={"X-Sova-Agent-Token": self.token}, body=body,
                                   timeout=timeout, limit=limit)

    def manifest(self, claim):
        return json.loads(self._call("GET", "/manifest", {"claim": str(claim)},
                                     timeout=900, limit=64 << 20))

    def read(self, claim, path, offset, length):
        return self._call("GET", "/read", {"claim": str(claim), "path": path,
                                           "offset": str(offset), "length": str(length)},
                          limit=length + 1024)

    def write(self, claim, path, offset, data):
        self._call("PUT", "/write", {"claim": str(claim), "path": path, "offset": str(offset)},
                   body=data)

    def commit(self, claim, partial, path, size, digest, mode):
        self._call("POST", "/commit", {"claim": str(claim), "partial": partial, "path": path,
                                       "size": str(size), "sha256": digest, "mode": str(mode)},
                   body=b"", timeout=600)

    def mkdir(self, claim, path, mode):
        self._call("POST", "/mkdir", {"claim": str(claim), "path": path, "mode": str(mode)},
                   body=b"")

    def symlink(self, claim, path, target):
        self._call("POST", "/symlink", {"claim": str(claim), "path": path, "target": target},
                   body=b"")

    def remove(self, claim, path):
        self._call("POST", "/remove", {"claim": str(claim), "path": path}, body=b"",
                   timeout=600)

    def chmod(self, claim, path, mode):
        self._call("POST", "/chmod", {"claim": str(claim), "path": path, "mode": str(mode)},
                   body=b"")


def _differences(source, target, limit=20):
    """Where two manifests differ: the first entries of each kind of difference."""
    want, have = {e[0]: e for e in source}, {e[0]: e for e in target}
    out = []
    for path in sorted(set(want) | set(have)):
        if want.get(path) != have.get(path):
            out.append({"path": path, "source": want.get(path), "target": have.get(path)})
            if len(out) >= limit:
                break
    return out


def _manifest_digest(entries):
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


class KubernetesVolumeHandoffAdapter:
    def __init__(self, adapter_id, *, source, target, route, routes, agent_key, image, run_as,
                 fs_group=None, storage_class=None, app_replicas=1, settle_seconds=60.0,
                 start_seconds=120.0, chunk_bytes=4 << 20):
        """``routes`` is the route value of (source, target); ``agent_key`` (at
        least 32 bytes, from the worker's secrets) derives each agent's token, so
        any worker instance can talk to an agent another one started."""
        if not isinstance(source, VolumeSide) or not isinstance(target, VolumeSide) or \
                len(source.claims) != len(target.claims):
            raise ContractError("A source and a target side with as many claims required")
        if not all(callable(getattr(route, n, None)) for n in ("get", "set")):
            raise ContractError("A route store required")
        if type(routes) is not tuple or len(routes) != 2 or routes[0] == routes[1]:
            raise ContractError("Distinct source and target route values required")
        if type(agent_key) is not bytes or len(agent_key) < 32:
            raise ContractError("An agent key of at least 32 bytes required")
        if type(image) is not str or not _IMAGE.fullmatch(image):
            raise ContractError("An agent image pinned by digest required")
        if type(run_as) is not tuple or len(run_as) != 2 or not all(
                type(v) is int and 1 <= v <= 2**31 - 1 for v in run_as):
            raise ContractError("A non-root user and group for the agent required")
        if fs_group is not None and (type(fs_group) is not int or not 1 <= fs_group < 2**31):
            raise ContractError("A numeric file system group required")
        if type(app_replicas) is not int or not 1 <= app_replicas <= 100:
            raise ContractError("Bounded application replicas required")
        if type(chunk_bytes) is not int or not 64 << 10 <= chunk_bytes <= 16 << 20:
            raise ContractError("A chunk between 64 KiB and 16 MiB required")
        self.adapter_id, self.route, self.routes = adapter_id, route, routes
        self.sides = {"source": source, "target": target}
        self.agent_key, self.image, self.run_as = agent_key, image, run_as
        self.fs_group, self.storage_class = fs_group, storage_class
        self.app_replicas, self.chunk_bytes = app_replicas, chunk_bytes
        self.settle_seconds, self.start_seconds = settle_seconds, start_seconds
        self.unit = hashlib.sha256(json.dumps(
            [adapter_id, *sorted(f"{s.namespace}/{','.join(s.claims)}"
                                 for s in self.sides.values())]).encode()).hexdigest()[:12]

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported handoff action for this adapter")

    def apply(self, command):
        getattr(self, "_" + command.action)()

    def readback(self, command, now):
        holds, detail = getattr(self, "_check_" + command.action)()
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))

    # -- observation -------------------------------------------------------------
    @staticmethod
    def _wait(predicate, seconds, step=0.5):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)
        return True

    def _claims(self, side):
        s = self.sides[side]
        return {name: s.controls.claim(name) for name in s.claims}

    def _bound(self, side):
        found = self._claims(side)
        return all(c is not None and (c.get("status") or {}).get("phase") == "Bound"
                   for c in found.values()), found

    def _mounting(self, side, pods=None):
        """Every Pod that may still write a claim of ``side``: name, node, mode."""
        s = self.sides[side]
        pods = s.controls.all_pods() if pods is None else pods
        out = []
        for pod in pods:
            mode = mounts(pod, s.claims)
            if mode is not None and alive(pod):
                out.append({"pod": pod["metadata"]["name"], "mode": mode,
                            "node": (pod.get("spec") or {}).get("nodeName"),
                            "labels": pod["metadata"].get("labels") or {}})
        return out

    def _writer_state(self, side, ref, pods):
        """Whether a registered writer is stopped: wants and runs no Pod."""
        kind, name = ref
        controls = self.sides[side].controls
        found = controls.workload(kind, name)
        if found is None:
            return {"writer": f"{kind}/{name}", "exists": False, "stopped": True}
        spec, status = found.get("spec") or {}, found.get("status") or {}
        if kind == "cronjob":
            suspended = spec.get("suspend") is True
            running = controls.running_jobs_of(found["metadata"]["uid"])
            return {"writer": f"{kind}/{name}", "exists": True, "suspended": suspended,
                    "running_jobs": running[:20], "stopped": suspended and not running}
        selector = (spec.get("selector") or {})
        if set(selector) - {"matchLabels"} or not selector.get("matchLabels"):
            raise ContractError("Only equality label selectors are decided")
        living = sorted(p["metadata"]["name"] for p in pods
                        if alive(p) and matches(p, selector["matchLabels"]))
        wanted = spec.get("replicas")
        return {"writer": f"{kind}/{name}", "exists": True, "desired": wanted,
                "replicas": status.get("replicas") or 0, "pods_alive": living[:20],
                "stopped": wanted == 0 and not status.get("replicas") and not living}

    def _stopped(self, side, writers):
        pods = self.sides[side].controls.all_pods()
        states = [self._writer_state(side, ref, pods) for ref in writers]
        return all(s["stopped"] for s in states), states, pods

    def _stop(self, side, writers):
        controls = self.sides[side].controls
        for kind, name in writers:
            found = controls.workload(kind, name)
            if found is None:
                continue
            version = found["metadata"]["resourceVersion"]
            if kind == "cronjob":
                if (found.get("spec") or {}).get("suspend") is not True:
                    controls.set_suspended(name, version)
            elif (found.get("spec") or {}).get("replicas") != 0:
                controls.set_replicas(kind, name, version, 0)
        # Stopping is asynchronous: wait, bounded, until no Pod of them runs.
        self._wait(lambda: self._stopped(side, writers)[0], self.settle_seconds)

    # -- agents ----------------------------------------------------------------
    def _token(self, side, name):
        return hmac.new(self.agent_key, f"{self.sides[side].namespace}/{name}".encode(),
                        hashlib.sha256).hexdigest()

    def _agent_pod(self, side, name, writable, node):
        s = self.sides[side]
        token_digest = hashlib.sha256(self._token(side, name).encode()).hexdigest()
        security = {"runAsNonRoot": True, "runAsUser": self.run_as[0],
                    "runAsGroup": self.run_as[1], "seccompProfile": {"type": "RuntimeDefault"}}
        if writable and self.fs_group is not None:
            security["fsGroup"] = self.fs_group  # a target is ours to populate
        spec = {
            "restartPolicy": "Never", "activeDeadlineSeconds": 3600,
            "terminationGracePeriodSeconds": 0, "automountServiceAccountToken": False,
            "enableServiceLinks": False, "securityContext": security,
            "containers": [{
                "name": "agent", "image": self.image, "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "-I", "-c", AGENT_SOURCE],
                "env": [{"name": "SOVA_AGENT_MODE", "value": "writer" if writable else "reader"},
                        {"name": "SOVA_AGENT_TOKEN_SHA256", "value": token_digest},
                        {"name": "SOVA_AGENT_CLAIMS", "value": str(len(s.claims))}],
                "ports": [{"containerPort": AGENT_PORT}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": AGENT_PORT},
                                   "periodSeconds": 1, "failureThreshold": 3},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"memory": "512Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [{"name": f"claim-{i}", "mountPath": f"/v/{i}",
                                  "readOnly": not writable} for i in range(len(s.claims))]}],
            "volumes": [{"name": f"claim-{i}", "persistentVolumeClaim": {
                "claimName": claim, "readOnly": not writable}}
                for i, claim in enumerate(s.claims)]}
        if node is not None:  # a single-node (ReadWriteOnce) volume already in use
            spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution":
                                {"nodeSelectorTerms": [{"matchExpressions": [{
                                    "key": "kubernetes.io/hostname", "operator": "In",
                                    "values": [node]}]}]}}}
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": name, "labels": {"app.kubernetes.io/managed-by": "sova",
                                     "sova.dev/volume-unit": self.unit,
                                     "sova.dev/volume-role": "writer" if writable else "reader"},
            "annotations": {"sova.dev/agent-sha256": AGENT_DIGEST}}, "spec": spec}

    def _start(self, side, writable):
        s = self.sides[side]
        if writable:  # a writer agent another worker left behind is never legitimate
            for name in s.controls.pods({"sova.dev/volume-unit": self.unit,
                                         "sova.dev/volume-role": "writer"}):
                s.controls.delete_pod(name)
        nodes = {m["node"] for m in self._mounting(side) if m["node"]}
        name = f"sova-vol-{self.unit[:10]}-{'w' if writable else 'r'}-{secrets.token_hex(4)}"
        try:
            s.controls.create_pod(self._agent_pod(side, name, writable,
                                                  next(iter(nodes)) if len(nodes) == 1
                                                  else None))
        except KubernetesRequestRefused as refused:
            raise ContractError(f"Volume agent refused by the cluster ({refused.status})") \
                from refused
        return side, name

    def _ready(self, side, name):
        controls = self.sides[side].controls
        deadline = time.monotonic() + self.start_seconds
        while True:
            pod = controls.pod(name)
            if pod is not None and ready(pod):
                return _Agent(controls, name, self._token(side, name))
            status = (pod or {}).get("status") or {}
            waiting = [((c.get("state") or {}).get("waiting") or {}).get("reason")
                       for c in status.get("containerStatuses") or ()]
            if pod is None or status.get("phase") in ("Failed", "Succeeded") or \
                    set(waiting) & _WAITING:
                raise ContractError(f"Volume agent did not start ({status.get('phase')}, "
                                    f"{[w for w in waiting if w]})")
            if time.monotonic() >= deadline:
                raise ContractError("Volume agent did not become ready in time")
            time.sleep(0.25)

    @contextmanager
    def _agents(self, *wanted):
        """Agents for ``(side, writable)`` pairs, started together, always reaped."""
        started = []
        try:
            for side, writable in wanted:
                started.append(self._start(side, writable))
            yield [self._ready(side, name) for side, name in started]
        finally:
            for side, name in started:
                self.sides[side].controls.delete_pod(name)
            for side, name in started:
                controls = self.sides[side].controls
                self._wait(lambda: controls.pod(name) is None, 60, 0.25)

    # -- copying ---------------------------------------------------------------
    def _transfer(self, source, target, claim, entry, live):
        path, _, mode, _, _, size, digest = entry
        directory, _, _ = path.rpartition("/")
        partial = PARTIAL + hashlib.sha256(path.encode()).hexdigest()[:24]
        partial_path = f"{directory}/{partial}" if directory else partial
        try:
            if size == 0:
                target.write(claim, partial_path, 0, b"")
            offset = 0
            while offset < size:
                data = source.read(claim, path, offset, min(self.chunk_bytes, size - offset))
                if not data:
                    break  # shorter than listed: the commit refuses it below
                target.write(claim, partial_path, offset, data)
                offset += len(data)
            target.commit(claim, partial, path, size, digest, mode)
            return True
        except KubernetesRequestRefused as refused:
            if refused.status != 409:
                raise
            if not live:
                raise ContractError(f"Source file changed during the copy: {path}") from refused
            return False  # changed while the source still serves: the final copy takes it

    def _sync(self, source, target, *, live):
        """Make each target claim's tree equal to the source's; returns a report."""
        report = {"copied_files": 0, "copied_bytes": 0, "removed": 0, "changed_later": 0}
        for claim in range(len(self.sides["source"].claims)):
            want = source.manifest(claim)
            if want["unsupported"]:
                raise ContractError(f"Source entries that cannot be copied: "
                                    f"{want['unsupported'][:5]}")
            have = target.manifest(claim)
            wanted = {e[0]: e for e in want["entries"]}
            present = {e[0]: e for e in have["entries"]}
            # Directories the agent cannot enter or write are opened first.
            for path, entry in present.items():
                if entry[1] == "d" and entry[2] & 0o700 != 0o700 and path in wanted:
                    target.chmod(claim, path, entry[2] | 0o700)
            gone = []
            for path in sorted(present):
                if any(path.startswith(g + "/") for g in gone):
                    continue
                entry = wanted.get(path)
                if entry is None or entry[1] != present[path][1]:
                    target.remove(claim, path)
                    report["removed"] += 1
                    gone.append(path)
            for path in gone:
                present.pop(path, None)
            present = {p: e for p, e in present.items()
                       if not any(p.startswith(g + "/") for g in gone)}
            for path in sorted(wanted):
                entry, current = wanted[path], present.get(path)
                if entry[1] == "d":
                    if current is None:
                        target.mkdir(claim, path, 0o700)
                elif entry[1] == "l":
                    if current is None or current[6] != entry[6]:
                        target.symlink(claim, path, entry[6])
                elif current is not None and current[5:] == entry[5:]:
                    if current[2] != entry[2]:
                        target.chmod(claim, path, entry[2])
                elif self._transfer(source, target, claim, entry, live):
                    report["copied_files"] += 1
                    report["copied_bytes"] += entry[5]
                else:
                    report["changed_later"] += 1
            # Directory modes last, deepest first, so the agent could fill them.
            for path in sorted((p for p, e in wanted.items() if e[1] == "d"), reverse=True):
                current = present.get(path)
                if current is None or current[2] != wanted[path][2] or \
                        current[2] & 0o700 != 0o700:
                    target.chmod(claim, path, wanted[path][2])
        return report

    def _compare(self):
        """Both sides' manifests, through read-only agents on bound claims only."""
        bound = {side: self._bound(side)[0] for side in self.sides}
        if not all(bound.values()):
            return False, {"claims_bound": bound}
        with self._agents(("source", False), ("target", False)) as (source, target):
            claims, equal = [], True
            for claim in range(len(self.sides["source"].claims)):
                want, have = source.manifest(claim), target.manifest(claim)
                same = want["entries"] == have["entries"] and not want["unsupported"]
                equal = equal and same
                claims.append({
                    "source_claim": self.sides["source"].claims[claim],
                    "target_claim": self.sides["target"].claims[claim],
                    "source": {"manifest": _manifest_digest(want["entries"]),
                               "files": want["files"], "bytes": want["bytes"]},
                    "target": {"manifest": _manifest_digest(have["entries"]),
                               "files": have["files"], "bytes": have["bytes"]},
                    "unsupported": want["unsupported"][:20],
                    "differences": [] if same else _differences(want["entries"],
                                                                have["entries"])})
        return equal, {"claims": claims, "agent_sha256": AGENT_DIGEST}

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        source, target = self.sides["source"], self.sides["target"]
        for source_name, target_name in zip(source.claims, target.claims):
            if target.controls.claim(target_name) is not None:
                continue
            original = source.controls.claim(source_name)
            if original is None:
                raise ContractError(f"Source claim {source_name} does not exist")
            spec = original.get("spec") or {}
            if spec.get("volumeMode", "Filesystem") != "Filesystem":
                raise ContractError("Only file system volumes are moved")
            target.controls.create_claim(
                target_name, access_modes=list(spec.get("accessModes") or ["ReadWriteOnce"]),
                storage=(spec.get("resources") or {}).get("requests", {}).get("storage"),
                storage_class=self.storage_class,
                labels={"app.kubernetes.io/managed-by": "sova",
                        "sova.dev/volume-unit": self.unit})
        self._stop("target", target.writers)
        if not self._bound("target")[0]:
            with self._agents(("target", True)):
                pass  # its first consumer binds a claim that waits for one
            self._wait(lambda: self._bound("target")[0], self.settle_seconds)

    def _check_stage_target(self):
        return self._staged("target", "source")

    def _staged(self, side, other):
        bound, found = self._bound(side)
        if not all(c is not None for c in found.values()):
            return False, {"claims": {n: c is not None for n, c in found.items()}}
        sizes = {}
        for mine, theirs in zip(self.sides[side].claims, self.sides[other].claims):
            wanted = ((self.sides[other].controls.claim(theirs) or {}).get("spec") or {}) \
                .get("resources", {}).get("requests", {}).get("storage")
            has = ((found[mine].get("status") or {}).get("capacity") or {}).get("storage")
            sizes[mine] = {"needed": wanted, "capacity": has,
                           "enough": bool(wanted and has and quantity(has) >= quantity(wanted))}
        stopped, writers, pods = self._stopped(side, self.sides[side].writers)
        writing = [m for m in self._mounting(side, pods) if m["mode"] == "rw"]
        holds = bound and all(s["enough"] for s in sizes.values()) and stopped and not writing
        return holds, {"claims_bound": bound, "sizes": sizes, "writers": writers,
                       "writing_pods": [m["pod"] for m in writing][:20]}

    def _pause_admission(self):
        self._stop("source", (self.sides["source"].app,))

    def _check_pause_admission(self):
        stopped, writers, _ = self._stopped("source", (self.sides["source"].app,))
        return stopped, {"application_writer": writers[0]}

    def _fence_source_writes(self):
        self._stop("source", self.sides["source"].writers)

    def _check_fence_source_writes(self):
        return self._fenced("source")

    def _fenced(self, side):
        stopped, writers, pods = self._stopped(side, self.sides[side].writers)
        writing = [m for m in self._mounting(side, pods) if m["mode"] == "rw"]
        return stopped and not writing, {
            "writers": writers, "writing_pods": [
                {"pod": m["pod"], "node": m["node"]} for m in writing][:20]}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        return self._staged("target", "source")

    def _start_sync(self):
        with self._agents(("source", False), ("target", True)) as (source, target):
            report = self._sync(source, target, live=True)
        stamp = json.dumps({"from": [self.sides["source"].namespace,
                                     list(self.sides["source"].claims)],
                            "at": datetime.now(UTC).isoformat(), **report},
                           separators=(",", ":"))
        controls = self.sides["target"].controls
        for name in self.sides["target"].claims:
            claim = controls.claim(name)
            controls.annotate_claim(name, claim["metadata"]["resourceVersion"], SYNCED, stamp)

    def _check_start_sync(self):
        found = self._claims("target")
        origin = [self.sides["source"].namespace, list(self.sides["source"].claims)]
        marked = {}
        for name, claim in found.items():
            note = ((claim or {}).get("metadata", {}).get("annotations") or {}).get(SYNCED)
            try:
                marked[name] = note is not None and json.loads(note)["from"] == origin
            except (ValueError, KeyError, TypeError):
                marked[name] = False
        return all(marked.values()), {"synced_claims": marked}

    def _copy_content(self):
        with self._agents(("source", False), ("target", True)) as (source, target):
            self._sync(source, target, live=False)

    def _check_copy_content(self):
        return self._compare()

    def _apply_final_changes(self):
        self._copy_content()

    def _check_apply_final_changes(self):
        return self._compare()

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        return self._compare()

    def _activate_target(self):
        target = self.sides["target"]
        self._stop("target", target.others)
        kind, name = target.app
        found = target.controls.workload(kind, name)
        if found is None:
            raise ContractError(f"Target application writer {kind}/{name} does not exist")
        if kind == "cronjob":
            raise ContractError("The application writer must be a Deployment or StatefulSet")
        if (found.get("spec") or {}).get("replicas") != self.app_replicas:
            target.controls.set_replicas(kind, name, found["metadata"]["resourceVersion"],
                                         self.app_replicas)
        self._wait(lambda: self._check_activate_target()[0], self.start_seconds)

    def _check_activate_target(self):
        target = self.sides["target"]
        kind, name = target.app
        found = target.controls.workload(kind, name)
        pods = target.controls.all_pods()
        selector = ((found or {}).get("spec") or {}).get("selector", {}).get("matchLabels") or {}
        app_pods = {p["metadata"]["name"] for p in pods if selector and matches(p, selector)}
        serving = sorted(p["metadata"]["name"] for p in pods
                         if p["metadata"]["name"] in app_pods and ready(p))
        writing = [m for m in self._mounting("target", pods) if m["mode"] == "rw"]
        foreign = sorted(m["pod"] for m in writing if m["pod"] not in app_pods)
        mounted = sorted(m["pod"] for m in writing if m["pod"] in app_pods and
                         m["pod"] in serving)
        others = [self._writer_state("target", ref, pods) for ref in target.others]
        fenced, source = self._fenced("source")
        wanted = ((found or {}).get("spec") or {}).get("replicas")
        holds = (wanted == self.app_replicas and bool(mounted) and not foreign
                 and all(o["stopped"] for o in others) and fenced)
        return holds, {"application_writer": f"{kind}/{name}", "desired": wanted,
                       "serving_pods": mounted[:20], "foreign_writing_pods": foreign[:20],
                       "other_writers": others, "source": source}

    def _commit_route(self):
        self.route.set(self.routes[1])

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.routes[1], {"route": current}
