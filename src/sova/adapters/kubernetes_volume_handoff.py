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
  Hard links inside a claim are kept: a group of paths naming one file is
  written once and linked, and a link the source does not have is broken.
  A file reaches its final name only after its content was checked against
  the source's SHA-256; if the source changes during the final copy, the fence
  did not hold and the copy fails.
* Content is compared as a manifest of every entry (path, kind, mode, owner,
  size, SHA-256 or link target) and of the hard links between them. Equal
  manifests are byte-identical trees with the same files shared.
  Ownership is not changed by the agent (it never runs as root): it writes as
  the registered user and group, the application's own, and a source whose
  files belong to someone else reads back as different, not as copied.
* ``start_sync`` copies while the source still serves, so the final copy
  inside the write stop moves only what changed since. A file that changes
  while it is copied is not written (a database in use often is: SQLite
  rewrites it as each connection closes); up to ``sync_passes`` passes copy
  what changed again, each only that. What still changed is left to the
  final copy, after the fence, so before the stop the target may lack a file
  or hold one older than its neighbours. It is recorded, with the passes and
  what was left, as an annotation on each target claim.
* Activation scales the target application writer to its registered replicas
  and waits, bounded, until a Pod of it is ready. Every other target writer
  stays stopped, and no Pod but the application's mounts a target claim
  read-write.

Agent Pods are reaped after each action or readback (and end by themselves
after an hour at the latest). A readback starts read-only agents only on
claims that are already bound, so it never changes what it reads. Every
action is idempotent; a postcondition that does not hold reads back UNKNOWN.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import time

from sova.adapters.evidence_bounds import bounded
from sova.adapters.kubernetes.volume_agents import (
    AGENT_DIGEST,
    VolumeAgents,
    differences,
    manifest_digest,
    same_tree,
    sync_trees,
)
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
SYNCED = "sova.dev/synced-from"


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


class KubernetesVolumeHandoffAdapter:
    def __init__(self, adapter_id, *, source, target, route, routes, agent_key, image, run_as,
                 fs_group=None, storage_class=None, app_replicas=1, settle_seconds=60.0,
                 start_seconds=120.0, chunk_bytes=4 << 20, agent_transport="proxy",
                 sync_passes=3):
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
        if type(app_replicas) is not int or not 1 <= app_replicas <= 100:
            raise ContractError("Bounded application replicas required")
        if type(chunk_bytes) is not int or not 64 << 10 <= chunk_bytes <= 16 << 20:
            raise ContractError("A chunk between 64 KiB and 16 MiB required")
        if type(sync_passes) is not int or not 1 <= sync_passes <= 10:
            raise ContractError("Between 1 and 10 sync passes")
        self.sync_passes = sync_passes
        self.adapter_id, self.route, self.routes = adapter_id, route, routes
        self.sides = {"source": source, "target": target}
        self.storage_class = storage_class
        self.app_replicas, self.chunk_bytes = app_replicas, chunk_bytes
        self.settle_seconds, self.start_seconds = settle_seconds, start_seconds
        self.unit = hashlib.sha256(json.dumps(
            [adapter_id, *sorted(f"{s.namespace}/{','.join(s.claims)}"
                                 for s in self.sides.values())]).encode()).hexdigest()[:12]
        self.agents = VolumeAgents(label=self.unit, key=agent_key, image=image, run_as=run_as,
                                   fs_group=fs_group, start_seconds=start_seconds,
                                   transport=agent_transport)

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

    # -- agents and copying --------------------------------------------------------
    def _agents(self, *wanted):
        """Agents for ``(side, writable)`` pairs, started together, always reaped."""
        return self.agents.running(*((self.sides[side].controls, self.sides[side].claims,
                                      writable) for side, writable in wanted))

    def _sync(self, source, target, *, live):
        return sync_trees(source, target, len(self.sides["source"].claims),
                          chunk=self.chunk_bytes, live=live)

    def _compare(self):
        """Both sides' manifests, through read-only agents on bound claims only."""
        bound = {side: self._bound(side)[0] for side in self.sides}
        if not all(bound.values()):
            return False, {"claims_bound": bound}
        with self._agents(("source", False), ("target", False)) as (source, target):
            claims, equal = [], True
            for claim in range(len(self.sides["source"].claims)):
                want, have = source.manifest(claim), target.manifest(claim)
                same = same_tree(want, have) and not want["unsupported"]
                equal = equal and same
                claims.append({
                    "source_claim": self.sides["source"].claims[claim],
                    "target_claim": self.sides["target"].claims[claim],
                    "source": {"manifest": manifest_digest(want["entries"], want["links"]),
                               "files": want["files"], "bytes": want["bytes"],
                               "link_groups": len(want["links"])},
                    "target": {"manifest": manifest_digest(have["entries"], have["links"]),
                               "files": have["files"], "bytes": have["bytes"],
                               "link_groups": len(have["links"])},
                    "unsupported": want["unsupported"][:20],
                    "differences": [] if same else differences(
                        want["entries"], have["entries"], source_links=want["links"],
                        target_links=have["links"])})
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
            report["passes"] = 1
            while report["changed_later"] and report["passes"] < self.sync_passes:
                again = self._sync(source, target, live=True)  # only what changed
                report = {**{k: report[k] + again[k] for k in ("copied_files", "copied_bytes",
                                                              "removed", "linked")},
                          "changed_later": again["changed_later"],
                          "passes": report["passes"] + 1}
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
