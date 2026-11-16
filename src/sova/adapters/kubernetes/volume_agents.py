"""Agent Pods over one side's claims, and the difference copy between two of them.

Shared by the volume handoff adapter and its independent probes: a
``VolumeAgents`` starts the ``volume_agent`` program in short-lived Pods
(read-only or read-write mounts, a scratch ``emptyDir``), waits until they are
ready, talks to them through the API server's Pod proxy and always reaps them.
Each agent's token is derived from a key and the Pod's namespace and name, so
any worker instance can reach an agent another one started; the Pod spec
carries only the token's digest.
"""

from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import secrets
import time

from sova.adapters.kubernetes import volume_agent as _agent_module
from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.kubernetes.volume_controls import alive, mounts, ready
from sova.domain.contracts import ContractError

AGENT_PORT = 8080
PARTIAL = ".sova-partial-"
_IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?"
                    r"@sha256:[0-9a-f]{64}|[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")
_WAITING = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                      "CreateContainerConfigError", "CreateContainerError", "CrashLoopBackOff"})
_SIZE = re.compile(r"[0-9]{1,6}(Mi|Gi)")

with open(_agent_module.__file__, encoding="utf-8") as _source:
    AGENT_SOURCE = _source.read()
AGENT_DIGEST = hashlib.sha256(AGENT_SOURCE.encode()).hexdigest()


class Agent:
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

    def sqlite(self, claim, path, queries=None):
        """Integrity, schema and per-table row digests of a SQLite database,
        read from a copy in the agent's scratch space (``queries`` answered there)."""
        return json.loads(self._call("POST", "/sqlite", {"claim": str(claim), "path": path},
                                     body=json.dumps({"queries": queries or {}}).encode(),
                                     timeout=900, limit=16 << 20))

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


def single_node(controls, claims):
    """The one node where the claims are in use, if exactly one: a second Pod of a
    single-node (ReadWriteOnce) volume must run there."""
    nodes = {(p.get("spec") or {}).get("nodeName") for p in controls.all_pods()
             if mounts(p, claims) is not None and alive(p)} - {None}
    return next(iter(nodes)) if len(nodes) == 1 else None


class VolumeAgents:
    """Start, reach and reap agent Pods for one unit (``label`` tells them apart)."""

    def __init__(self, *, label, key, image, run_as, fs_group=None, start_seconds=120.0,
                 scratch="1Gi"):
        if type(key) is not bytes or len(key) < 32:
            raise ContractError("An agent key of at least 32 bytes required")
        if type(image) is not str or not _IMAGE.fullmatch(image):
            raise ContractError("An agent image pinned by digest required")
        if type(run_as) is not tuple or len(run_as) != 2 or not all(
                type(v) is int and 1 <= v <= 2**31 - 1 for v in run_as):
            raise ContractError("A non-root user and group for the agent required")
        if fs_group is not None and (type(fs_group) is not int or not 1 <= fs_group < 2**31):
            raise ContractError("A numeric file system group required")
        if type(scratch) is not str or not _SIZE.fullmatch(scratch):
            raise ContractError("A scratch size in Mi or Gi required")
        if type(label) is not str or not re.fullmatch(r"[a-z0-9]{6,20}", label):
            raise ContractError("A short agent label required")
        self.label, self.key, self.image, self.run_as = label, key, image, run_as
        self.fs_group, self.start_seconds, self.scratch = fs_group, start_seconds, scratch

    def token(self, controls, name):
        return hmac.new(self.key, f"{controls.registration.namespace}/{name}".encode(),
                        hashlib.sha256).hexdigest()

    def pod(self, controls, claims, name, writable, node):
        digest = hashlib.sha256(self.token(controls, name).encode()).hexdigest()
        security = {"runAsNonRoot": True, "runAsUser": self.run_as[0],
                    "runAsGroup": self.run_as[1], "seccompProfile": {"type": "RuntimeDefault"}}
        if writable and self.fs_group is not None:
            security["fsGroup"] = self.fs_group  # a volume being filled is ours to set up
        spec = {
            "restartPolicy": "Never", "activeDeadlineSeconds": 3600,
            "terminationGracePeriodSeconds": 0, "automountServiceAccountToken": False,
            "enableServiceLinks": False, "securityContext": security,
            "containers": [{
                "name": "agent", "image": self.image, "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "-I", "-c", AGENT_SOURCE],
                "env": [{"name": "SOVA_AGENT_MODE", "value": "writer" if writable else "reader"},
                        {"name": "SOVA_AGENT_TOKEN_SHA256", "value": digest},
                        {"name": "SOVA_AGENT_CLAIMS", "value": str(len(claims))},
                        {"name": "SOVA_AGENT_SCRATCH", "value": "/scratch"}],
                "ports": [{"containerPort": AGENT_PORT}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": AGENT_PORT},
                                   "periodSeconds": 1, "failureThreshold": 3},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"memory": "512Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [*({"name": f"claim-{i}", "mountPath": f"/v/{i}",
                                    "readOnly": not writable} for i in range(len(claims))),
                                 {"name": "scratch", "mountPath": "/scratch"}]}],
            "volumes": [*({"name": f"claim-{i}", "persistentVolumeClaim": {
                "claimName": claim, "readOnly": not writable}}
                for i, claim in enumerate(claims)),
                {"name": "scratch", "emptyDir": {"sizeLimit": self.scratch}}]}
        if node is not None:
            spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution":
                                {"nodeSelectorTerms": [{"matchExpressions": [{
                                    "key": "kubernetes.io/hostname", "operator": "In",
                                    "values": [node]}]}]}}}
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": name, "labels": {"app.kubernetes.io/managed-by": "sova",
                                     "sova.dev/volume-unit": self.label,
                                     "sova.dev/volume-role": "writer" if writable else "reader"},
            "annotations": {"sova.dev/agent-sha256": AGENT_DIGEST}}, "spec": spec}

    def start(self, controls, claims, writable):
        if writable:  # a writer agent another worker left behind is never legitimate
            for name in controls.pods({"sova.dev/volume-unit": self.label,
                                       "sova.dev/volume-role": "writer"}):
                controls.delete_pod(name)
        name = f"sova-vol-{self.label[:10]}-{'w' if writable else 'r'}-{secrets.token_hex(4)}"
        try:
            controls.create_pod(self.pod(controls, claims, name, writable,
                                         single_node(controls, claims)))
        except KubernetesRequestRefused as refused:
            raise ContractError(f"Volume agent refused by the cluster ({refused.status})") \
                from refused
        return name

    def ready(self, controls, name):
        deadline = time.monotonic() + self.start_seconds
        while True:
            pod = controls.pod(name)
            if pod is not None and ready(pod):
                return Agent(controls, name, self.token(controls, name))
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
    def running(self, *wanted):
        """Agents for ``(controls, claims, writable)``, started together, always reaped."""
        started = []
        try:
            for controls, claims, writable in wanted:
                started.append((controls, self.start(controls, claims, writable)))
            yield [self.ready(controls, name) for controls, name in started]
        finally:
            for controls, name in started:
                controls.delete_pod(name)
            for controls, name in started:
                deadline = time.monotonic() + 60
                while controls.pod(name) is not None and time.monotonic() < deadline:
                    time.sleep(0.25)


def differences(source, target, limit=20):
    """Where two manifests differ: the first differing entries, in path order."""
    want, have = {e[0]: e for e in source}, {e[0]: e for e in target}
    out = []
    for path in sorted(set(want) | set(have)):
        if want.get(path) != have.get(path):
            out.append({"path": path, "source": want.get(path), "target": have.get(path)})
            if len(out) >= limit:
                break
    return out


def manifest_digest(entries):
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _transfer(source, target, claim, entry, *, chunk, live):
    path, _, mode, _, _, size, digest = entry
    directory, _, _ = path.rpartition("/")
    partial = PARTIAL + hashlib.sha256(path.encode()).hexdigest()[:24]
    partial_path = f"{directory}/{partial}" if directory else partial
    try:
        if size == 0:
            target.write(claim, partial_path, 0, b"")
        offset = 0
        while offset < size:
            data = source.read(claim, path, offset, min(chunk, size - offset))
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


def sync_trees(source, target, claims, *, chunk=4 << 20, live=False):
    """Make each of ``claims`` target trees equal to the source's; a report.

    Only what differs is written: entries whose kind changed or that the source
    no longer has are removed, files whose size or content differ are copied
    and checked, modes are set. ``live``: the source still serves, so a file
    that changes during its copy is left for the final copy instead of failing.
    """
    report = {"copied_files": 0, "copied_bytes": 0, "removed": 0, "changed_later": 0}
    for claim in range(claims):
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
        present = {p: e for p, e in present.items()
                   if p not in gone and not any(p.startswith(g + "/") for g in gone)}
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
            elif _transfer(source, target, claim, entry, chunk=chunk, live=live):
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
