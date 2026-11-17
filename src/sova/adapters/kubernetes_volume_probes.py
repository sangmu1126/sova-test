"""Independent probes for a Kubernetes volume handoff (§16.3, §15.5, §28.2).

The measurements behind a volume unit's obligations, made apart from the
adapter that moved the files, and where possible by another method than the
adapter's own readback:

* Write denial is an attempt, not a reading of controller fields: each live
  Pod of a registered writer is asked to write through the application's own
  write-probe endpoint (a write it undoes); a Pod that cannot be reached
  counts as able to write. On top of that, no Pod outside the registered
  writers may mount a claim read-write, and the side's tree must not change
  over a short observation window - a write nobody registered still shows.
* Content of a SQLite database is compared logically, from copies in the
  agent's scratch space: integrity check, schema, and each table's rows as a
  multiset (the adapter compares bytes). Other protected sets are compared as
  the manifest of their subtree.
* A business invariant is a registered read-only query over a database of
  the unit: either rows that violate it (none allowed) or file names it
  refers to (each must exist on the target).
* The restore drill copies the target into a fresh scratch claim with its own
  agents and compares the copy with the target: every entry, and what SQLite
  reads from each database, whatever state the target holds it in.
* Cross-tenant isolation: no claim of the target namespace is bound to the
  volume of another tenant's registered claim (the way a released volume is
  taken over), read from both namespaces with their own registrations.
* Placement comes from ``zone_of(side)``: the provider's or the operator's
  inventory of where the side's volumes are, reported as read.
* Transport: the API server reaches agent Pods over the cluster network
  without TLS, so a requirement for verified transport encryption is measured
  as not met, never assumed.

Each probe returns ``(passed, observation)``.
"""

import time

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.kubernetes.volume_agents import manifest_digest, sync_trees
from sova.adapters.kubernetes.volume_controls import alive, matches, mounts, ready
from sova.domain.contracts import ContractError


class KubernetesVolumeProbes:
    def __init__(self, *, source, target, agents, write_probe=None, databases=(),
                 invariants=None, zone_of=None, other=None, settle_seconds=2.0,
                 kubelet_tls_verified=None):
        """``source``/``target`` are the adapter's ``VolumeSide`` values;
        ``agents`` a ``VolumeAgents`` with a label of the probes' own;
        ``write_probe`` the application's ``(port, path)`` that writes and undoes
        it; ``other`` another tenant's ``(controls, claim)``."""
        self.sides = {"source": source, "target": target}
        self.agents, self.databases = agents, tuple(databases)
        self.invariants = dict(invariants or {})
        if write_probe is not None and (type(write_probe) is not tuple or len(write_probe) != 2):
            raise ContractError("Write probe as (port, path)")
        self.write_probe, self.zone_of, self.other = write_probe, zone_of, other
        self.settle_seconds = settle_seconds
        self.kubelet_tls_verified = kubelet_tls_verified  # side -> the inventory's word

    # -- helpers ---------------------------------------------------------------
    def _locate(self, name):
        """(claim index, path) of a protected set: ``<claim>:<path>`` or a path in
        the first claim."""
        claims = self.sides["target"].claims
        if ":" in name:
            claim, _, path = name.partition(":")
            if claim not in self.sides["source"].claims:
                raise ContractError("Protected set names an unregistered claim")
            return self.sides["source"].claims.index(claim), path
        return 0, name if claims else None

    def _readers(self, *sides):
        return self.agents.running(*((self.sides[s].controls, self.sides[s].claims, False)
                                     for s in sides))

    def _try_write(self, controls, pod):
        if self.write_probe is None:
            return "running"  # a live writer process with no probe to ask
        port, path = self.write_probe
        try:
            controls.proxy("POST", pod["metadata"]["name"], port, path, {}, headers={},
                           body=b"", timeout=15)
            return "write_accepted"
        except KubernetesRequestRefused as refused:
            # 502-504: the API server could not reach the Pod; it may still write.
            return "unreachable" if refused.status in (502, 503, 504) else "write_refused"

    def _writer_outcomes(self, side, pods):
        s = self.sides[side]
        outcomes, registered = {}, set()
        for kind, name in s.writers:
            found = s.controls.workload(kind, name)
            key = f"{kind}/{name}"
            if found is None:
                outcomes[key] = ["absent"]
                continue
            spec = found.get("spec") or {}
            if kind == "cronjob":
                running = s.controls.running_jobs_of(found["metadata"]["uid"])
                outcomes[key] = ["running_jobs" if running else
                                 "suspended" if spec.get("suspend") is True else "scheduled"]
                continue
            selector = (spec.get("selector") or {}).get("matchLabels") or {}
            living = [p for p in pods if selector and alive(p) and matches(p, selector)]
            registered |= {p["metadata"]["name"] for p in living}
            if not living:
                outcomes[key] = ["wants_pods" if spec.get("replicas") else "no_pod"]
                continue
            outcomes[key] = sorted({self._try_write(s.controls, p) for p in living})
        return outcomes, registered

    def _manifests(self, agent, side):
        return [manifest_digest(agent.manifest(i)["entries"])
                for i in range(len(self.sides[side].claims))]

    # -- write paths -------------------------------------------------------------
    def write_denied(self, side):
        s = self.sides[side]
        pods = s.controls.all_pods()
        outcomes, registered = self._writer_outcomes(side, pods)
        denied = all(set(o) <= {"absent", "no_pod", "suspended", "write_refused"}
                     for o in outcomes.values())
        unregistered = sorted(p["metadata"]["name"] for p in pods
                              if alive(p) and mounts(p, s.claims) == "rw"
                              and p["metadata"]["name"] not in registered)
        bound = all((c or {}).get("status", {}).get("phase") == "Bound"
                    for c in (s.controls.claim(n) for n in s.claims))
        unchanged, digests = None, None
        if bound:
            with self._readers(side) as (agent,):
                first = self._manifests(agent, side)
                time.sleep(self.settle_seconds)
                second = self._manifests(agent, side)
            unchanged, digests = first == second, {"first": first, "second": second}
        passed = denied and not unregistered and bound and unchanged
        return passed, {"probe": "write_denied", "side": side, "outcomes": outcomes,
                        "unregistered_writers": unregistered[:20], "claims_bound": bound,
                        "unchanged_over_seconds": self.settle_seconds,
                        "unchanged": unchanged, "manifests": digests}

    def single_writer(self):
        t = self.sides["target"]
        pods = t.controls.all_pods()
        kind, name = t.app
        found = t.controls.workload(kind, name) or {}
        selector = (found.get("spec") or {}).get("selector", {}).get("matchLabels") or {}
        serving = [p for p in pods if selector and matches(p, selector) and ready(p)
                   and mounts(p, t.claims) == "rw"]
        target = sorted({self._try_write(t.controls, p) for p in serving}) or ["no_pod"]
        source_denied, source = self.write_denied("source")
        wrote = target == ["write_accepted"] or (self.write_probe is None and
                                                 target == ["running"])
        return wrote and source_denied, {"probe": "single_writer", "target": target,
                                         "source": source["outcomes"],
                                         "source_unregistered": source["unregistered_writers"]}

    # -- content -----------------------------------------------------------------
    @staticmethod
    def _subtree(entries, path):
        return [e for e in entries if e[0] == path or e[0].startswith(path + "/")]

    def _database_equal(self, source, target, claim, path):
        a, b = source.sqlite(claim, path), target.sqlite(claim, path)
        same = (a["integrity"] == ["ok"] == b["integrity"] and a["schema"] == b["schema"]
                and a["tables"] == b["tables"])
        return same, {"source": {"integrity": a["integrity"], "schema": a["schema"],
                                 "tables": a["tables"]},
                      "target": {"integrity": b["integrity"], "schema": b["schema"],
                                 "tables": b["tables"]}}

    def content_equal(self, name):
        claim, path = self._locate(name)
        with self._readers("source", "target") as (source, target):
            if path in self.databases:
                same, detail = self._database_equal(source, target, claim, path)
                return same, {"probe": "content_equal", "set": name, "method": "sqlite_logical",
                              **detail}
            want = self._subtree(source.manifest(claim)["entries"], path)
            have = self._subtree(target.manifest(claim)["entries"], path)
        same = bool(want) and want == have
        return same, {"probe": "content_equal", "set": name, "method": "manifest",
                      "source": {"entries": len(want), "manifest": manifest_digest(want)},
                      "target": {"entries": len(have), "manifest": manifest_digest(have)}}

    def invariant(self, invariant_id):
        spec = self.invariants.get(invariant_id)
        if spec is None:
            return False, {"probe": "invariant", "invariant": invariant_id,
                           "reason": "no probe registered for this invariant"}
        claim, database = self._locate(spec["database"])
        query = spec.get("violations") or spec.get("files")
        with self._readers("target") as (target,):
            report = target.sqlite(claim, database, {"q": query})
            files = ({e[0] for e in target.manifest(claim)["entries"] if e[1] == "f"}
                     if "files" in spec else None)
        answer = report["results"]["q"]
        if "error" in answer or answer["truncated"] or report["integrity"] != ["ok"]:
            return False, {"probe": "invariant", "invariant": invariant_id,
                           "integrity": report["integrity"], "answer": answer}
        if "violations" in spec:
            rows = answer["rows"]
            return not rows, {"probe": "invariant", "invariant": invariant_id,
                              "violations": len(rows), "first": rows[:5]}
        under = spec.get("under", "").strip("/")
        missing = sorted(str(r[0]) for r in answer["rows"]
                         if r and (f"{under}/{r[0]}" if under else str(r[0])) not in files)
        return not missing, {"probe": "invariant", "invariant": invariant_id,
                             "referenced": len(answer["rows"]), "missing": missing[:20]}

    # -- placement, isolation, transport ------------------------------------------------
    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}

    def cross_tenant_denied(self, other=None):
        if self.other is None:
            return False, {"probe": "cross_tenant_denied", "reason": "no other tenant registered"}
        controls, claim = self.other
        theirs = controls.claim(claim) or {}
        volume = (theirs.get("spec") or {}).get("volumeName")
        if not volume or (theirs.get("status") or {}).get("phase") != "Bound":
            return False, {"probe": "cross_tenant_denied",
                           "reason": "the other tenant's claim is not bound"}
        taken = sorted(c["metadata"]["name"] for c in self.sides["target"].controls.all_claims()
                       if (c.get("spec") or {}).get("volumeName") == volume)
        return not taken, {"probe": "cross_tenant_denied", "other_volume_bound_here": taken}

    def transport_encrypted(self, side):
        """How the data reaches the side's agents, hop by hop. Through the Pod
        proxy the API server reaches the Pod over the cluster network in the
        clear. Through the port-forward the bytes stay inside this client's
        verified TLS to the API server and the API server's TLS to the kubelet,
        then within the node; whether the API server verifies the kubelet's
        certificate is the cluster's setting, which this client cannot see:
        it is the inventory's word (``kubelet_tls_verified``), said as such."""
        if self.agents.transport != "port-forward":
            return False, {"probe": "transport_encrypted", "side": side, "encrypted": False,
                           "transport": "proxy",
                           "reason": "agent Pods are reached by the API server over the "
                                     "cluster network without TLS"}
        kubelet = bool(self.kubelet_tls_verified and self.kubelet_tls_verified(side))
        return kubelet, {"probe": "transport_encrypted", "side": side, "encrypted": True,
                         "transport": "port-forward",
                         "hops": {"client_to_api_server": "verified TLS (registered CA)",
                                  "api_server_to_kubelet": "TLS, verified per inventory"
                                  if kubelet else "TLS, verification not recorded",
                                  "kubelet_to_pod": "within the node"},
                         "client_verification": "verify-identity" if kubelet else "partial",
                         "source": "inventory" if self.kubelet_tls_verified else "none"}

    # -- restore -----------------------------------------------------------------------
    def restore_drill(self, scratch):
        """Copy the target into fresh scratch claims and compare the copy with it:
        every entry, and what SQLite reads from each registered database.

        The question is whether a restore reproduces what the target holds,
        not whether that is consistent: inside the write stop the content
        comparison requires an intact database on both sides. Before the stop
        (snapshot-cdc) the target may hold a copy of a database still in use,
        torn or not there yet; the evidence shows its integrity as found."""
        t = self.sides["target"]
        names = tuple(f"{scratch}-{i}" for i in range(len(t.claims)))
        try:
            for name, original in zip(names, t.claims):
                spec = (t.controls.claim(original) or {}).get("spec") or {}
                if t.controls.claim(name) is None:
                    t.controls.create_claim(
                        name, access_modes=list(spec.get("accessModes") or ["ReadWriteOnce"]),
                        storage=spec["resources"]["requests"]["storage"],
                        storage_class=spec.get("storageClassName"),
                        labels={"app.kubernetes.io/managed-by": "sova",
                                "sova.dev/volume-unit": self.agents.label})
            with self.agents.running((t.controls, t.claims, False),
                                     (t.controls, names, True)) as (target, copy):
                report = sync_trees(target, copy, len(t.claims), live=False)
                equal, sets = True, []
                for i in range(len(t.claims)):
                    want, have = target.manifest(i)["entries"], copy.manifest(i)["entries"]
                    equal = equal and want == have
                    sets.append({"claim": t.claims[i], "manifest": manifest_digest(want),
                                 "restored": manifest_digest(have)})
                databases = {}
                for database in self.databases:  # what SQLite reads, on both
                    claim, path = self._locate(database)
                    held, restored = _held(target, claim, path), _held(copy, claim, path)
                    same = held == restored
                    equal = equal and same
                    databases[database] = {
                        "equal": same, "present": held is not None,
                        "target_integrity": None if held is None else held["integrity"],
                        "restored_integrity": None if restored is None else
                        restored["integrity"]}
        except ContractError as error:
            return False, {"probe": "restore_drill", "reason": str(error)[:300]}
        finally:
            for name in names:
                t.controls.delete_claim(name)
        return equal, {"probe": "restore_drill", "claims": sets, "databases": databases,
                       "copied_files": report["copied_files"],
                       "copied_bytes": report["copied_bytes"]}


def _held(agent, claim, path):
    """A database's logical content on one claim, or None where it is absent."""
    try:
        report = agent.sqlite(claim, path)
    except KubernetesRequestRefused as error:
        if error.status == 409 and error.reason == "missing":
            return None
        raise
    return {key: report[key] for key in ("integrity", "schema", "tables")}
