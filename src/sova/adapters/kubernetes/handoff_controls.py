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
  ``deployment_paused`` does the same for a controller that runs as a
  Deployment (an operator, a sync agent): no replica wanted, none running,
  no pod of it alive. Something that scales it back (a GitOps sync) makes the
  next measurement fail.

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
_SECRET_KEY = re.compile(r"[-._a-zA-Z0-9]{1,253}")
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

    def set_service_selector(self, name, resource_version, selector, *, object_uid=None):
        operations = ([{"op": "test", "path": "/metadata/uid", "value": object_uid}]
                      if object_uid is not None else [])
        return self._patch(self._ns("/api/v1", "services", name), resource_version,
                           [*operations, {"op": "replace", "path": "/spec/selector",
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

    def secret(self, name):
        """The Secret object; its values stay in memory, never in a message."""
        return self._get(self._ns("/api/v1", "secrets", name))

    def set_secret_value(self, name, resource_version, key, encoded):
        if type(key) is not str or not _SECRET_KEY.fullmatch(key):
            raise ContractError("Kubernetes Secret data key required")
        if type(encoded) is not str:
            raise ContractError("Base64 Secret value required")
        pointer = "/data/" + key.replace("~", "~0").replace("/", "~1")
        current = self.secret(name)
        operations = ([{"op": "add", "path": pointer, "value": encoded}]
                      if current.get("data") is not None
                      else [{"op": "add", "path": "/data", "value": {key: encoded}}])
        return self._patch(self._ns("/api/v1", "secrets", name), resource_version, operations)

    def deployment(self, name):
        return self._get(self._ns("/apis/apps/v1", "deployments", name))

    def scale_deployment(self, name, resource_version, replicas):
        if type(replicas) is not int or not 0 <= replicas <= 1000:
            raise ContractError("Bounded replica count required")
        return self._patch(self._ns("/apis/apps/v1", "deployments", name), resource_version,
                           [{"op": "replace", "path": "/spec/replicas", "value": replicas}])

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

    def __init__(self, controls, service, sides, *, converge_seconds=30.0,
                 observe=None, samples=3, expected_service_uid=None):
        if not isinstance(controls, KubernetesHandoffControls):
            raise ContractError("Registered Kubernetes controls required")
        if type(sides) is not dict or len(sides) < 2:
            raise ContractError("A selector for each side required")
        self.controls, self.service = controls, _name(service)
        self.sides = {ref: _selector(selector) for ref, selector in sides.items()}
        if len({json.dumps(s) for s in self.sides.values()}) != len(self.sides):
            raise ContractError("Sides must have distinct selectors")
        if observe is not None and (not callable(observe) or type(samples) is not int
                                    or not 1 <= samples <= 8):
            raise ContractError("A callable HTTP observer and 1..8 samples required")
        if expected_service_uid is not None and (type(expected_service_uid) is not str
                or not 1 <= len(expected_service_uid) <= 128
                or any(c.isspace() for c in expected_service_uid)):
            raise ContractError("An exact Service UID required")
        self.expected_service_uid = expected_service_uid
        self.http_observe, self.samples = observe, samples
        self.converge_seconds = converge_seconds

    def _service(self):
        current = self.controls.service(self.service)
        if self.expected_service_uid is not None and current.get("metadata", {}).get("uid") != self.expected_service_uid:
            raise ContractError("Registered Service was replaced")
        return current

    def _select(self, current, selector):
        if self.expected_service_uid is not None:
            self.controls.set_service_selector(self.service, current["metadata"]["resourceVersion"],
                                               selector, object_uid=self.expected_service_uid)
        else:
            self.controls.set_service_selector(self.service, current["metadata"]["resourceVersion"], selector)

    def observe(self):
        """What traffic can reach now, and the side that alone is behind it."""
        current = self._service()
        selector = (current.get("spec") or {}).get("selector")
        side = next((ref for ref, wanted in self.sides.items() if wanted == selector), None)
        endpoints = self.controls.endpoints(self.service)
        backing = self.controls.pods(self.sides[side]) if side is not None else set()
        foreign = sorted({str(pod) for pod, _ in endpoints if pod not in backing})
        ready = sorted(pod for pod, ok in endpoints if ok and pod in backing)
        decided = side if side is not None and ready and not foreign else None
        observation = {"selector_side": side, "ready_endpoints": len(ready),
                       "foreign_endpoints": foreign[:20], "route": decided}
        if self.http_observe is not None and decided is not None:
            answers = [self.http_observe() for _ in range(self.samples)]
            observation["http_sides"] = answers
            if any(answer != decided for answer in answers):
                observation["route"] = None
            if self.expected_service_uid is not None:
                after = self._service()
                if after["metadata"].get("resourceVersion") != current["metadata"].get("resourceVersion"):
                    observation["route"] = None
                    observation["service_changed_during_probe"] = True
        return observation

    def get(self):
        return self.observe()["route"]

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        current = self._service()
        if (current.get("spec") or {}).get("selector") != self.sides[ref]:
            self._select(current, self.sides[ref])
        # Traffic follows the selector only once the endpoints are rebuilt; wait,
        # bounded, so the readback that follows sees the settled route.
        deadline = time.monotonic() + self.converge_seconds
        while self.get() != ref and time.monotonic() < deadline:
            time.sleep(0.25)

    # -- pausing: new traffic reaches no side, open connections keep flowing -------------
    def pause(self, ref):
        """Point the Service at ``ref``, a side whose selector matches no Pod."""
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        current = self._service()
        if (current.get("spec") or {}).get("selector") != self.sides[ref]:
            self._select(current, self.sides[ref])

    def paused(self, ref):
        """(paused, what traffic reaches): the paused side's selector, no endpoint."""
        seen = self.observe()
        return (seen["selector_side"] == ref and seen["ready_endpoints"] == 0
                and not seen["foreign_endpoints"]), seen

    def left(self, ref):
        """The route no longer selects side ``ref`` (paused or moved on)."""
        return self.observe()["selector_side"] not in (ref, None)


def controller_paused(controls, cronjob):
    """``controller.<id>.paused`` for a CronJob: suspended, and none of its Jobs
    still running. Returns ``(passed, observation)``."""
    job = controls.cronjob(cronjob)
    suspended = (job.get("spec") or {}).get("suspend") is True
    running = controls.running_jobs_of(job["metadata"]["uid"])
    return suspended and not running, {"probe": "controller_paused", "controller": cronjob,
                                       "suspended": suspended, "running_jobs": running[:20]}


def deployment_paused(controls, deployment):
    """``controller.<id>.paused`` for a controller that runs as a Deployment (an
    operator, a sync agent): it wants no replica, has none, and no pod of it is
    still alive. A selector beyond plain label equality is not guessed at."""
    found = controls.deployment(deployment)
    selector = (found.get("spec") or {}).get("selector") or {}
    if set(selector) - {"matchLabels"} or not selector.get("matchLabels"):
        raise ContractError("Only equality label selectors are decided")
    wanted = (found.get("spec") or {}).get("replicas")
    status = (found.get("status") or {}).get("replicas") or 0
    alive = sorted(controls.pods(selector["matchLabels"]))
    return wanted == 0 and status == 0 and not alive, {
        "probe": "controller_paused", "controller": deployment, "desired": wanted,
        "replicas": status, "pods_alive": alive[:20]}


def pause_deployment(controls, deployment):
    """The approved pause itself: scale the controller's Deployment to zero."""
    found = controls.deployment(deployment)
    if (found.get("spec") or {}).get("replicas") != 0:
        controls.scale_deployment(deployment, found["metadata"]["resourceVersion"], 0)


def pause_cronjob(controls, cronjob):
    """The approved pause itself: suspend the CronJob (new runs only)."""
    job = controls.cronjob(cronjob)
    if (job.get("spec") or {}).get("suspend") is not True:
        controls.suspend_cronjob(cronjob, job["metadata"]["resourceVersion"])
