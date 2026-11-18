"""Agent Pods over one side's claims, and the difference copy between two of them.

Shared by the volume handoff adapter and its independent probes: a
``VolumeAgents`` starts the ``volume_agent`` program in short-lived Pods
(read-only or read-write mounts, a scratch ``emptyDir``), waits until they are
ready, talks to them through the API server's Pod proxy and always reaps them.
Each agent's token is derived from a key and the Pod's namespace and name, so
any worker instance can reach an agent another one started; the Pod spec
carries only the token's digest.

With ``keep``, a reader agent is not reaped after its use but kept for the
next use of the same claims by the same ``VolumeAgents`` - the steps and
probes of one execution follow one another within seconds, and on a managed
cluster each new agent costs seconds (scheduling, attaching the volume to its
node). Only agents this object started are reused, never one found in the
cluster; a writer is never kept, nor any agent on a ReadWriteOncePod claim
(it admits one Pod at a time); a use that fails reaps what it used; and
``release()`` reaps every kept agent - the execution's end. Every agent also
ends by itself after ``idle_seconds`` without a request, so one whose worker
is gone lets go of its volumes; a later start removes its finished Pod.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import secrets
import threading
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


TRANSPORTS = ("proxy", "port-forward")


class Agent:
    """Requests to one agent Pod through the API server: its Pod proxy (the
    API server reaches the Pod over the cluster network in the clear) or its
    port-forward (the bytes stay inside TLS to the kubelet)."""

    def __init__(self, controls, name, token, transport="proxy"):
        self.controls, self.name, self.token = controls, name, token
        self.transport = transport

    def _call(self, method, path, query, *, body=None, timeout=120, limit=1 << 20):
        call = self.controls.forward if self.transport == "port-forward" else \
            self.controls.proxy
        return call(method, self.name, AGENT_PORT, path, query,
                    headers={"X-Sova-Agent-Token": self.token}, body=body,
                    timeout=timeout, limit=limit)

    def healthy(self):
        """Whether the agent answers now (asked directly, not the kubelet's probe)."""
        try:
            self._call("GET", "/healthz", {}, timeout=2, limit=1024)
            return True
        except Exception:  # noqa: BLE001 - not up yet, whatever the reason
            return False

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

    def link(self, claim, existing, path):
        self._call("POST", "/link", {"claim": str(claim), "existing": existing, "path": path},
                   body=b"")

    def remove(self, claim, path):
        self._call("POST", "/remove", {"claim": str(claim), "path": path}, body=b"",
                   timeout=600)

    def chmod(self, claim, path, mode):
        self._call("POST", "/chmod", {"claim": str(claim), "path": path, "mode": str(mode)},
                   body=b"")


def beside(controls, claims):
    """Where a second Pod of a single-node (ReadWriteOnce) volume must run: beside
    the live Pod that holds the claims, if they are held on exactly one node.
    ``{"labels": ...}`` names that Pod by all its labels, for a Pod affinity on
    the host; ``{"node": ...}`` only for a holder without labels. GKE Autopilot
    refuses a node affinity on the host name, a Pod affinity it admits
    (measured)."""
    holders = [p for p in controls.all_pods() if mounts(p, claims) is not None and alive(p)
               and (p.get("spec") or {}).get("nodeName")]
    nodes = {p["spec"]["nodeName"] for p in holders}
    if len(nodes) != 1:
        return None
    labelled = [p for p in holders if (p.get("metadata") or {}).get("labels")]
    if labelled:
        return {"labels": dict(labelled[0]["metadata"]["labels"])}
    return {"node": next(iter(nodes))}


class VolumeAgents:
    """Start, reach and reap agent Pods for one unit (``label`` tells them apart)."""

    def __init__(self, *, label, key, image, run_as, fs_group=None, start_seconds=120.0,
                 scratch="1Gi", transport="proxy", keep=False, idle_seconds=600):
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
        if transport not in TRANSPORTS:
            raise ContractError("Agent transport: proxy or port-forward")
        if type(keep) is not bool:
            raise ContractError("keep: true or false")
        if type(idle_seconds) is not int or not 120 <= idle_seconds <= 3000:
            raise ContractError("Agent idle seconds between 120 and 3000")
        self.transport, self.keep, self.idle_seconds = transport, keep, idle_seconds
        self._kept, self._single_pod, self._lock = {}, {}, threading.Lock()
        self.label, self.key, self.image, self.run_as = label, key, image, run_as
        self.fs_group, self.start_seconds, self.scratch = fs_group, start_seconds, scratch

    def token(self, controls, name):
        return hmac.new(self.key, f"{controls.registration.namespace}/{name}".encode(),
                        hashlib.sha256).hexdigest()

    def pod(self, controls, claims, name, writable, place):
        digest = hashlib.sha256(self.token(controls, name).encode()).hexdigest()
        security = {"runAsNonRoot": True, "runAsUser": self.run_as[0],
                    "runAsGroup": self.run_as[1], "seccompProfile": {"type": "RuntimeDefault"}}
        if writable and self.fs_group is not None:
            security["fsGroup"] = self.fs_group  # a volume being filled is ours to set up
            # The kubelet changes ownership of every file at each mount by default;
            # once the volume's root is ours, each later mount skips that walk.
            security["fsGroupChangePolicy"] = "OnRootMismatch"
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
                        {"name": "SOVA_AGENT_SCRATCH", "value": "/scratch"},
                        {"name": "SOVA_AGENT_IDLE_SECONDS", "value": str(self.idle_seconds)}],
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
        if place and "labels" in place:
            spec["affinity"] = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                {"labelSelector": {"matchLabels": place["labels"]},
                 "topologyKey": "kubernetes.io/hostname"}]}}
        elif place:
            spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution":
                                {"nodeSelectorTerms": [{"matchExpressions": [{
                                    "key": "kubernetes.io/hostname", "operator": "In",
                                    "values": [place["node"]]}]}]}}}
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": name, "labels": {"app.kubernetes.io/managed-by": "sova",
                                     "sova.dev/volume-unit": self.label,
                                     "sova.dev/volume-role": "writer" if writable else "reader"},
            "annotations": {"sova.dev/agent-sha256": AGENT_DIGEST}}, "spec": spec}

    def start(self, controls, claims, writable):
        mine = {"sova.dev/volume-unit": self.label}
        for pod in controls.all_pods(mine):  # ended by themselves (idle, deadline)
            if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                controls.delete_pod(pod["metadata"]["name"])
        if writable:  # a writer agent another worker left behind is never legitimate
            for name in controls.pods({**mine, "sova.dev/volume-role": "writer"}):
                controls.delete_pod(name)
        name = f"sova-vol-{self.label[:10]}-{'w' if writable else 'r'}-{secrets.token_hex(4)}"
        try:
            controls.create_pod(self.pod(controls, claims, name, writable,
                                         beside(controls, claims)))
        except KubernetesRequestRefused as refused:
            raise ContractError(f"Volume agent refused by the cluster ({refused.status})") \
                from refused
        return name

    def ready(self, controls, name):
        deadline = time.monotonic() + self.start_seconds
        while True:
            pod = controls.pod(name)
            if pod is not None and ready(pod):
                return Agent(controls, name, self.token(controls, name), self.transport)
            if pod is not None and _running(pod):
                # Up before the kubelet's next readiness probe (once a second): ask it.
                agent = Agent(controls, name, self.token(controls, name), self.transport)
                if agent.healthy():
                    return agent
            status = (pod or {}).get("status") or {}
            waiting = [((c.get("state") or {}).get("waiting") or {}).get("reason")
                       for c in status.get("containerStatuses") or ()]
            if pod is None or status.get("phase") in ("Failed", "Succeeded") or \
                    set(waiting) & _WAITING:
                raise ContractError(f"Volume agent did not start ({status.get('phase')}, "
                                    f"{[w for w in waiting if w]})")
            if time.monotonic() >= deadline:
                raise ContractError("Volume agent did not become ready in time")
            time.sleep(0.1)

    @staticmethod
    def _key(controls, claims):
        return (controls.registration.endpoint, controls.registration.namespace, tuple(claims))

    def _keepable(self, controls, claims):
        """A reader on claims that admit more than one Pod (not ReadWriteOncePod)."""
        key = self._key(controls, claims)
        if key not in self._single_pod:
            self._single_pod[key] = any(
                "ReadWriteOncePod" in (((controls.claim(c) or {}).get("spec") or {})
                                       .get("accessModes") or ()) for c in claims)
        return self.keep and not self._single_pod[key]

    def _reuse(self, controls, claims):
        """The reader this object kept for these claims, if it still serves and
        is well within its idle time."""
        with self._lock:
            kept = self._kept.pop(self._key(controls, claims), None)
        if kept is None:
            return None
        _, name, used = kept
        pod = controls.pod(name)
        # Running as when it was taken into use: the kubelet may not have marked
        # an agent ready yet that already answered (its probe runs once a second).
        serving = pod is not None and (ready(pod) or _running(pod))
        if not serving or time.monotonic() - used > self.idle_seconds - 60:
            if pod is not None:
                controls.delete_pod(name)
            return None
        return name

    @contextmanager
    def running(self, *wanted):
        """Agents for ``(controls, claims, writable)``, started together; reaped
        after use, except readers kept for the next use (``keep``)."""
        used, failed = [], True  # (controls, claims, name, keep, new)
        try:
            for controls, claims, writable in wanted:
                keep = not writable and self._keepable(controls, claims)
                name = self._reuse(controls, claims) if keep else None
                new = name is None
                if new:
                    name = self.start(controls, claims, writable)
                used.append((controls, claims, name, keep, new))
            agents = [self.ready(controls, name) if new else
                      Agent(controls, name, self.token(controls, name), self.transport)
                      for controls, _, name, _, new in used]
            yield agents
            failed = False
        finally:
            reaped = []
            for controls, claims, name, keep, _ in used:
                if keep and not failed:
                    with self._lock:
                        self._kept[self._key(controls, claims)] = (controls, name,
                                                                   time.monotonic())
                else:  # a use that failed leaves nothing for the next one
                    reaped.append((controls, name))
            _reap(reaped)

    def release(self):
        """Reap every kept agent (the end of an execution)."""
        with self._lock:
            kept, self._kept = self._kept, {}
        _reap([(controls, name) for controls, name, _ in kept.values()])


def _reap(agents, seconds=60):
    """Delete agent Pods and wait, bounded, until they are gone."""
    for controls, name in agents:
        controls.delete_pod(name)
    for controls, name in agents:
        deadline = time.monotonic() + seconds
        while controls.pod(name) is not None and time.monotonic() < deadline:
            time.sleep(0.25)


def remove_agents(controls, claims, seconds=60):
    """Remove every volume agent - of any unit or probe - that mounts one of
    ``claims``: before the application takes a single-node volume, nothing
    else may hold it on another node. Returns the names removed."""
    found = [p["metadata"]["name"] for p in controls.all_pods(
        {"app.kubernetes.io/managed-by": "sova"})
        if (p["metadata"].get("labels") or {}).get("sova.dev/volume-role") in
        ("reader", "writer") and mounts(p, claims) is not None]
    _reap([(controls, name) for name in found], seconds)
    return found


def _running(pod):
    status = pod.get("status") or {}
    return bool(status.get("podIP")) and not pod["metadata"].get("deletionTimestamp") and any(
        (c.get("state") or {}).get("running") for c in status.get("containerStatuses") or ())


def differences(source, target, limit=20, *, source_links=(), target_links=()):
    """Where two manifests differ: the first differing entries, in path order,
    then the first paths whose hard links differ (the other paths naming the
    same file, on each side)."""
    want, have = {e[0]: e for e in source}, {e[0]: e for e in target}
    out = []
    for path in sorted(set(want) | set(have)):
        if want.get(path) != have.get(path):
            out.append({"path": path, "source": want.get(path), "target": have.get(path)})
            if len(out) >= limit:
                return out
    linked = link_groups(source_links), link_groups(target_links)
    for path in sorted(set(linked[0]) | set(linked[1])):
        mine, theirs = linked[0].get(path, (path,)), linked[1].get(path, (path,))
        if mine != theirs:
            out.append({"path": path, "source_links": list(mine), "target_links": list(theirs)})
            if len(out) >= limit:
                break
    return out


def link_groups(links):
    """Each linked path -> the sorted paths naming its file."""
    return {path: tuple(group) for group in links for path in group}


def subtree_links(links, path):
    """The hard links of a manifest seen from the subtree at ``path``: each group's
    paths inside it, where any is (a file inside linked to one outside keeps a
    group of one, so the link to the outside still shows)."""
    out = []
    for group in links:
        inside = [p for p in group if p == path or p.startswith(path + "/")]
        if inside:
            out.append(inside)
    return out


def manifest_digest(entries, links=()):
    """A tree's digest: its entries, and its hard links where it has any (a tree
    without links digests as before links were listed)."""
    body = [entries, list(links)] if links else entries
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()


def same_tree(want, have):
    """Whether two manifests describe the same tree: every entry and every link."""
    return want["entries"] == have["entries"] and \
        want.get("links", []) == have.get("links", [])


class _Changed(Exception):
    """A source file changed while it was being copied."""


def _partial(path):
    directory, _, _ = path.rpartition("/")
    partial = PARTIAL + hashlib.sha256(path.encode()).hexdigest()[:24]
    return partial, f"{directory}/{partial}" if directory else partial


def _copy_files(source, target, claim, entries, *, chunk, live, streams):
    """Copy the content of ``entries`` with up to ``streams`` requests in flight;
    each file reaches its name only once all of it landed and was checked.
    Returns the paths copied and the paths that changed while live.

    In three rounds, each waited for: every file's first chunk (it creates or
    truncates the partial file, so it must land before the others), the
    remaining chunks, the commits."""
    changed, lock = set(), threading.Lock()

    def guarded(work, path):
        def run(*args):
            try:
                work(*args)
            except (_Changed, KubernetesRequestRefused) as error:
                if isinstance(error, KubernetesRequestRefused) and error.status != 409:
                    raise
                if not live:
                    raise ContractError(f"Source file changed during the copy: {path}") \
                        from error
                with lock:
                    changed.add(path)  # the final copy, inside the fence, takes it
        return run

    def piece(path, partial_path, offset, length):
        if path in changed:
            return
        data = source.read(claim, path, offset, length) if length else b""
        if len(data) != length:
            raise _Changed(path)
        target.write(claim, partial_path, offset, data)

    def finish(path, size, digest, mode):
        if path not in changed:
            target.commit(claim, _partial(path)[0], path, size, digest, mode)

    firsts, rest, commits = [], [], []
    for path, _, mode, _, _, size, digest in entries:
        partial_path = _partial(path)[1]
        firsts.append((guarded(piece, path), (path, partial_path, 0, min(chunk, size))))
        rest += [(guarded(piece, path), (path, partial_path, offset, min(chunk, size - offset)))
                 for offset in range(chunk, size, chunk)]
        commits.append((guarded(finish, path), (path, size, digest, mode)))
    with ThreadPoolExecutor(max_workers=streams) as pool:
        for round_ in (firsts, rest, commits):
            for future in [pool.submit(work, *args) for work, args in round_]:
                future.result()
    return [e for e in entries if e[0] not in changed], sorted(changed)


def sync_trees(source, target, claims, *, chunk=4 << 20, live=False, streams=4):
    """Make each of ``claims`` target trees equal to the source's; a report.

    Only what differs is written: entries whose kind changed or that the source
    no longer has are removed, files whose size or content differ are copied
    (``streams`` chunks in flight) and checked, modes are set. ``live``: the
    source still serves, so a file that changes during its copy is left for the
    final copy instead of failing.

    Hard links are kept: of each group of paths naming one source file, the
    first carries the content and the others are linked to it on the target. A
    target file that shares its file with a path outside its source group is
    written anew, so a link the source does not have is broken.
    """
    if type(streams) is not int or not 1 <= streams <= 16:
        raise ContractError("Between 1 and 16 copy streams")
    report = {"copied_files": 0, "copied_bytes": 0, "removed": 0, "linked": 0,
              "changed_later": 0}
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
        groups = link_groups(want.get("links", ()))
        shared = {p: {q for q in g if q in present}
                  for p, g in link_groups(have.get("links", ())).items() if p in present}
        to_copy, to_link = [], []
        for path in sorted(wanted):
            entry, current = wanted[path], present.get(path)
            group = groups.get(path, (path,))
            if entry[1] == "d":
                if current is None:
                    target.mkdir(claim, path, 0o700)
            elif entry[1] == "l":
                if current is None or current[6] != entry[6]:
                    target.symlink(claim, path, entry[6])
            elif path != group[0]:  # named by the group's first path's file
                to_link.append((group[0], path))
            elif current is not None and current[5:] == entry[5:] and \
                    shared.get(path, {path}) <= set(group):
                if current[2] != entry[2]:
                    target.chmod(claim, path, entry[2])
            else:
                to_copy.append(entry)
        copied, later = _copy_files(source, target, claim, to_copy, chunk=chunk, live=live,
                                    streams=streams)
        rewritten, held_back = {e[0] for e in copied}, set(later)
        for first, path in to_link:
            if first in held_back:
                continue  # its file is left for the final copy, and so is this name
            if path in shared and first in shared[path] and first not in rewritten:
                continue  # already names that file
            try:
                target.link(claim, first, path)
            except KubernetesRequestRefused as refused:
                if refused.status != 409 or not live:
                    raise
                later.append(path)
                continue
            report["linked"] += 1
        report["copied_files"] += len(copied)
        report["copied_bytes"] += sum(e[5] for e in copied)
        report["changed_later"] += len(later)
        # Directory modes last, deepest first, so the agent could fill them.
        for path in sorted((p for p, e in wanted.items() if e[1] == "d"), reverse=True):
            current = present.get(path)
            if current is None or current[2] != wanted[path][2] or \
                    current[2] & 0o700 != 0o700:
                target.chmod(claim, path, wanted[path][2])
    return report
