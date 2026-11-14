"""Traffic route and controller pause for a handoff, through the registered API.

Two things a handoff needs from the cluster that runs the application, on any
conformant Kubernetes (managed or on-prem):

* ``KubernetesServiceRoute`` is the route store a data adapter's
  ``commit_route`` writes to. It points a Service at one side's pods and reads
  the route back from what traffic can actually reach: the Service selector
  must be that side's, and every endpoint behind the Service must be one of
  that side's pods. While endpoints still converge, or while anything else
  (another pod, a hand-made endpoint) is behind the Service, the route reads
  back as no side at all.
* ``controller_paused`` measures a ``controller.<id>.paused`` obligation for a
  CronJob, the usual shape of a batch writer: it must be suspended and no Job
  it started may still run. Suspending stops new runs only, so a running Job
  keeps the obligation failing until it ends or the operator stops it.

Requests use only exact paths in the registered namespace, through the pinned
transport. Every change is a JSON Patch that first tests the object's
``resourceVersion``, so a change made by someone else in between is refused
instead of overwritten.
"""

import json
import re
import time

from sova.adapters.kubernetes.https import KubernetesHTTPS
from sova.domain.contracts import ContractError

_DNS = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LABEL_KEY = re.compile(r"(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?/)?"
                        r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
_LABEL_VALUE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?")
_TIMEOUT = 5


def _name(value):
    if type(value) is not str or not _DNS.fullmatch(value):
        raise ContractError("Kubernetes DNS label required")
    return value


def _selector(value):
    if type(value) is not dict or not 1 <= len(value) <= 16 or not all(
            type(k) is str and _LABEL_KEY.fullmatch(k) and type(v) is str
            and _LABEL_VALUE.fullmatch(v) for k, v in value.items()):
        raise ContractError("Bounded equality label selector required")
    return dict(sorted(value.items()))


def _query(selector):
    return ",".join(f"{k}={v}" for k, v in _selector(selector).items())


class KubernetesHandoffControls(KubernetesHTTPS):
    """Exact reads and guarded patches in the registered namespace."""

    def _ns(self, base, plural, name=None):
        path = f"{base}/namespaces/{self.registration.namespace}/{plural}"
        return path if name is None else f"{path}/{_name(name)}"

    def _get(self, path, query=None):
        return self._request("GET", path, query or {}, timeout=_TIMEOUT)

    def _patch(self, path, resource_version, operations):
        if type(resource_version) is not str or not resource_version:
            raise ContractError("Current resourceVersion required")
        body = json.dumps([{"op": "test", "path": "/metadata/resourceVersion",
                            "value": resource_version}, *operations]).encode()
        return self._request("PATCH", path, {}, timeout=_TIMEOUT, body=body)

    def service(self, name):
        return self._get(self._ns("/api/v1", "services", name))

    def set_service_selector(self, name, resource_version, selector):
        return self._patch(self._ns("/api/v1", "services", name), resource_version,
                           [{"op": "replace", "path": "/spec/selector",
                             "value": _selector(selector)}])

    def endpoints(self, service):
        """Every endpoint behind ``service``: (pod name or None, ready)."""
        slices = self._get(self._ns("/apis/discovery.k8s.io/v1", "endpointslices"),
                           {"labelSelector": "kubernetes.io/service-name=" + _name(service),
                            "limit": "64"})
        if slices.get("metadata", {}).get("continue"):
            raise ContractError("Too many endpoint slices to decide")
        found = []
        for piece in slices.get("items", ()):
            for endpoint in piece.get("endpoints") or ():
                ref = endpoint.get("targetRef") or {}
                pod = ref.get("name") if ref.get("kind") == "Pod" else None
                ready = (endpoint.get("conditions") or {}).get("ready") is not False
                found.append((pod, ready))
        return found

    def pods(self, selector):
        listed = self._get(self._ns("/api/v1", "pods"),
                           {"labelSelector": _query(selector), "limit": "256"})
        if listed.get("metadata", {}).get("continue"):
            raise ContractError("Too many pods to decide")
        return {p["metadata"]["name"] for p in listed.get("items", ())
                if not p["metadata"].get("deletionTimestamp")}

    def cronjob(self, name):
        return self._get(self._ns("/apis/batch/v1", "cronjobs", name))

    def suspend_cronjob(self, name, resource_version):
        return self._patch(self._ns("/apis/batch/v1", "cronjobs", name), resource_version,
                           [{"op": "add", "path": "/spec/suspend", "value": True}])

    def running_jobs_of(self, owner_uid):
        listed = self._get(self._ns("/apis/batch/v1", "jobs"), {"limit": "256"})
        if listed.get("metadata", {}).get("continue"):
            raise ContractError("Too many jobs to decide")
        running = []
        for job in listed.get("items", ()):
            owners = job["metadata"].get("ownerReferences") or ()
            if not any(o.get("uid") == owner_uid for o in owners):
                continue
            finished = any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                           for c in (job.get("status") or {}).get("conditions") or ())
            if not finished:
                running.append(job["metadata"]["name"])
        return sorted(running)


class KubernetesServiceRoute:
    """Route store: which side's pods a Service sends traffic to."""

    def __init__(self, controls, service, sides, *, converge_seconds=30.0):
        if not isinstance(controls, KubernetesHandoffControls):
            raise ContractError("Registered Kubernetes controls required")
        if type(sides) is not dict or len(sides) < 2:
            raise ContractError("A selector for each side required")
        self.controls, self.service = controls, _name(service)
        self.sides = {ref: _selector(selector) for ref, selector in sides.items()}
        if len({json.dumps(s) for s in self.sides.values()}) != len(self.sides):
            raise ContractError("Sides must have distinct selectors")
        self.converge_seconds = converge_seconds

    def observe(self):
        """What traffic can reach now, and the side that alone is behind it."""
        selector = (self.controls.service(self.service).get("spec") or {}).get("selector")
        side = next((ref for ref, wanted in self.sides.items() if wanted == selector), None)
        endpoints = self.controls.endpoints(self.service)
        backing = self.controls.pods(self.sides[side]) if side is not None else set()
        foreign = sorted({str(pod) for pod, _ in endpoints if pod not in backing})
        ready = sorted(pod for pod, ok in endpoints if ok and pod in backing)
        decided = side if side is not None and ready and not foreign else None
        return {"selector_side": side, "ready_endpoints": len(ready),
                "foreign_endpoints": foreign[:20], "route": decided}

    def get(self):
        return self.observe()["route"]

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        current = self.controls.service(self.service)
        if (current.get("spec") or {}).get("selector") != self.sides[ref]:
            self.controls.set_service_selector(
                self.service, current["metadata"]["resourceVersion"], self.sides[ref])
        # Traffic follows the selector only once the endpoints are rebuilt; wait,
        # bounded, so the readback that follows sees the settled route.
        deadline = time.monotonic() + self.converge_seconds
        while self.get() != ref and time.monotonic() < deadline:
            time.sleep(0.25)


def controller_paused(controls, cronjob):
    """``controller.<id>.paused`` for a CronJob: suspended, and none of its Jobs
    still running. Returns ``(passed, observation)``."""
    job = controls.cronjob(cronjob)
    suspended = (job.get("spec") or {}).get("suspend") is True
    running = controls.running_jobs_of(job["metadata"]["uid"])
    return suspended and not running, {"probe": "controller_paused", "controller": cronjob,
                                       "suspended": suspended, "running_jobs": running[:20]}


def pause_cronjob(controls, cronjob):
    """The approved pause itself: suspend the CronJob (new runs only)."""
    job = controls.cronjob(cronjob)
    if (job.get("spec") or {}).get("suspend") is not True:
        controls.suspend_cronjob(cronjob, job["metadata"]["resourceVersion"])
