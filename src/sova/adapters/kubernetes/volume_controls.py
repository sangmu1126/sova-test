"""Claims, writer workloads and agent Pods of one namespace, for a volume handoff.

Extends the registered handoff controls with what moving an application's
file state needs: exact reads and creation of PersistentVolumeClaims, the
writer workloads that mount them (Deployments, StatefulSets, CronJobs), every
Pod of the namespace with the claims it mounts, short-lived agent Pods, and
the API server's proxy to an agent Pod. Paths stay fixed to the registered
namespace; every change to an existing object is a guarded JSON Patch.
"""

from decimal import Decimal, InvalidOperation
import json
from urllib.parse import urlencode
import re

from sova.adapters.kubernetes.handoff_controls import (
    KubernetesHandoffControls,
    _name,
    _selector,
)
from sova.adapters.kubernetes.https import KubernetesRequestRefused, refusal_reason
from sova.domain.contracts import ContractError

KINDS = frozenset({"deployment", "statefulset", "cronjob"})
_PLURAL = {"deployment": ("/apis/apps/v1", "deployments"),
           "statefulset": ("/apis/apps/v1", "statefulsets"),
           "cronjob": ("/apis/batch/v1", "cronjobs")}
_TIMEOUT = 5
_FINISHED = frozenset({"Succeeded", "Failed"})
_QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?")
_UNITS = {None: 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15,
          "E": 10**18, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50,
          "Ei": 2**60}


def quantity(text):
    """Bytes of a Kubernetes storage quantity (``10Gi``, ``500M``)."""
    match = _QUANTITY.fullmatch(text) if type(text) is str else None
    if not match:
        raise ContractError("Plain storage quantity required")
    try:
        return int(Decimal(match[1]) * _UNITS[match[2]])
    except InvalidOperation as error:
        raise ContractError("Plain storage quantity required") from error


def workload_ref(value):
    if type(value) is not tuple or len(value) != 2 or value[0] not in KINDS:
        raise ContractError("Writer workload as (deployment|statefulset|cronjob, name)")
    return value[0], _name(value[1])


def mounts(pod, claims):
    """``"rw"``, ``"ro"`` or ``None``: how ``pod`` mounts any of ``claims``.

    A claim given to a container read-write, in any container (init and
    ephemeral ones included) or as a raw block device, is a read-write mount.
    """
    spec = pod.get("spec") or {}
    volumes = {}
    for volume in spec.get("volumes") or ():
        source = volume.get("persistentVolumeClaim") or {}
        if source.get("claimName") in claims:
            volumes[volume["name"]] = bool(source.get("readOnly"))
    if not volumes:
        return None
    for container in [*(spec.get("initContainers") or ()), *(spec.get("containers") or ()),
                      *(spec.get("ephemeralContainers") or ())]:
        for mount in container.get("volumeMounts") or ():
            if mount.get("name") in volumes and not volumes[mount["name"]] \
                    and not mount.get("readOnly"):
                return "rw"
        for device in container.get("volumeDevices") or ():
            if device.get("name") in volumes:
                return "rw"
    return "ro"


def alive(pod):
    """The Pod may still run a process: not finished (a terminating Pod counts)."""
    return (pod.get("status") or {}).get("phase") not in _FINISHED


def ready(pod):
    return alive(pod) and not pod["metadata"].get("deletionTimestamp") and any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in (pod.get("status") or {}).get("conditions") or ())


def matches(pod, selector):
    labels = pod["metadata"].get("labels") or {}
    return all(labels.get(k) == v for k, v in selector.items())


def _whole_answer(answer):
    """Whether an HTTP answer has all the body its Content-Length announces."""
    head, separator, rest = bytes(answer).partition(b"\r\n\r\n")
    if not separator:
        return False
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return len(rest) >= int(value.strip())
            except ValueError:
                return False
    return False


class KubernetesVolumeControls(KubernetesHandoffControls):
    """Exact reads, creations and guarded patches for a volume handoff."""

    # -- claims ----------------------------------------------------------------
    def claim(self, name):
        """The PersistentVolumeClaim, or ``None`` if it does not exist."""
        try:
            return self._get(self._ns("/api/v1", "persistentvolumeclaims", name))
        except KubernetesRequestRefused as refused:
            if refused.status == 404:
                return None
            raise

    def create_claim(self, name, *, access_modes, storage, storage_class, labels):
        if type(access_modes) is not list or not access_modes or not set(access_modes) <= {
                "ReadWriteOnce", "ReadWriteOncePod", "ReadWriteMany", "ReadOnlyMany"}:
            raise ContractError("Claim access modes required")
        quantity(storage)
        body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": _name(name), "labels": _selector(labels)},
                "spec": {"accessModes": access_modes, "volumeMode": "Filesystem",
                         "resources": {"requests": {"storage": storage}}}}
        if storage_class is not None:
            body["spec"]["storageClassName"] = _name(storage_class)
        return self._request("POST", self._ns("/api/v1", "persistentvolumeclaims"), {},
                             timeout=_TIMEOUT, body=json.dumps(body).encode(),
                             content_type="application/json", accept=(201,))

    def all_claims(self):
        listed = self._get(self._ns("/api/v1", "persistentvolumeclaims"), {"limit": "500"})
        if listed.get("metadata", {}).get("continue"):
            raise ContractError("Too many claims to decide")
        return listed.get("items", [])

    def delete_claim(self, name):
        try:
            self._request("DELETE", self._ns("/api/v1", "persistentvolumeclaims", name), {},
                          timeout=_TIMEOUT, accept=(200, 202))
        except KubernetesRequestRefused as refused:
            if refused.status != 404:
                raise

    def annotate_claim(self, name, resource_version, key, value):
        if type(key) is not str or not key.startswith("sova.dev/") or type(value) is not str \
                or len(value) > 1024:
            raise ContractError("Sova claim annotation required")
        current = self.claim(name) or {}
        pointer = "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")
        operations = ([{"op": "add", "path": pointer, "value": value}]
                      if current.get("metadata", {}).get("annotations") is not None
                      else [{"op": "add", "path": "/metadata/annotations",
                             "value": {key: value}}])
        return self._patch(self._ns("/api/v1", "persistentvolumeclaims", name),
                           resource_version, operations)

    # -- writer workloads --------------------------------------------------------
    def workload(self, kind, name):
        """The workload object, or ``None`` if it does not exist."""
        kind, name = workload_ref((kind, name))
        base, plural = _PLURAL[kind]
        try:
            return self._get(self._ns(base, plural, name))
        except KubernetesRequestRefused as refused:
            if refused.status == 404:
                return None
            raise

    def set_replicas(self, kind, name, resource_version, replicas):
        if kind not in ("deployment", "statefulset"):
            raise ContractError("Only Deployments and StatefulSets have replicas")
        if type(replicas) is not int or not 0 <= replicas <= 1000:
            raise ContractError("Bounded replica count required")
        base, plural = _PLURAL[kind]
        return self._patch(self._ns(base, plural, name), resource_version,
                           [{"op": "replace", "path": "/spec/replicas", "value": replicas}])

    def annotate_workload(self, kind, name, resource_version, key, value):
        if type(key) is not str or not key.startswith("sova.dev/") or type(value) is not str \
                or len(value) > 1024:
            raise ContractError("Sova workload annotation required")
        kind, name = workload_ref((kind, name))
        current = self.workload(kind, name) or {}
        pointer = "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")
        operations = ([{"op": "add", "path": pointer, "value": value}]
                      if current.get("metadata", {}).get("annotations") is not None
                      else [{"op": "add", "path": "/metadata/annotations",
                             "value": {key: value}}])
        base, plural = _PLURAL[kind]
        return self._patch(self._ns(base, plural, name), resource_version, operations)

    def add_ephemeral_container(self, pod, resource_version, container, *, first):
        """Attach ``container`` to a running Pod (it shares the Pod's network)."""
        if type(container) is not dict or not str(container.get("name", "")).startswith(
                "sova-"):
            raise ContractError("A Sova ephemeral container required")
        operation = ({"op": "add", "path": "/spec/ephemeralContainers", "value": [container]}
                     if first else
                     {"op": "add", "path": "/spec/ephemeralContainers/-", "value": container})
        return self._patch(self._ns("/api/v1", "pods", pod) + "/ephemeralcontainers",
                           resource_version, [operation])

    def container_log(self, pod, container, *, tail=1):
        """The last ``tail`` lines a container wrote, as text."""
        if type(tail) is not int or not 1 <= tail <= 100:
            raise ContractError("Bounded log tail required")
        raw = self._request("GET", self._ns("/api/v1", "pods", pod) + "/log",
                            {"container": _name(container), "tailLines": str(tail)},
                            timeout=_TIMEOUT, limit=64 << 10, raw=True)
        return raw.decode("utf-8", "replace")

    def set_suspended(self, name, resource_version):
        return self._patch(self._ns("/apis/batch/v1", "cronjobs", name), resource_version,
                           [{"op": "add", "path": "/spec/suspend", "value": True}])

    # -- pods ------------------------------------------------------------------
    def pod(self, name):
        try:
            return self._get(self._ns("/api/v1", "pods", name))
        except KubernetesRequestRefused as refused:
            if refused.status == 404:
                return None
            raise

    def all_pods(self, selector=None):
        """Every Pod of the namespace (terminating ones included), whole objects."""
        found, cursor = [], None
        for _ in range(16):
            query = {"limit": "500"}
            if selector:
                query["labelSelector"] = ",".join(
                    f"{k}={v}" for k, v in _selector(selector).items())
            if cursor:
                query["continue"] = cursor
            page = self._get(self._ns("/api/v1", "pods"), query)
            found.extend(page.get("items", ()))
            cursor = page.get("metadata", {}).get("continue")
            if not cursor:
                return found
        raise ContractError("Too many pods to decide")

    def create_pod(self, body):
        return self._request("POST", self._ns("/api/v1", "pods"), {}, timeout=_TIMEOUT,
                             body=json.dumps(body).encode(), content_type="application/json",
                             accept=(201,))

    def delete_pod(self, name):
        try:
            self._request("DELETE", self._ns("/api/v1", "pods", name),
                          {"gracePeriodSeconds": "0"}, timeout=_TIMEOUT, accept=(200, 202))
        except KubernetesRequestRefused as refused:
            if refused.status != 404:
                raise

    def forward(self, method, pod, port, path, query, *, headers, body=None, timeout=60,
                limit=1 << 20):
        """A request to ``pod:port`` through the API server's port-forward,
        answered as bytes: the same exchange as ``proxy`` without the bytes
        crossing the cluster network in the clear."""
        if type(port) is not int or not 1 <= port <= 65535 or type(path) is not str \
                or not path.startswith("/") or ".." in path or method not in (
                    "GET", "POST", "PUT") or (body is not None and type(body) is not bytes):
            raise ContractError("Exact agent request required")
        if any(type(k) is not str or not k.startswith("X-Sova-") or type(v) is not str
               or any(ord(c) < 33 or ord(c) > 126 for c in v) for k, v in headers.items()):
            raise ContractError("Exact agent request required")
        body = body or b""
        target = path + ("?" + urlencode(query) if query else "")
        lines = [f"{method} {target} HTTP/1.1", "Host: localhost", "Connection: close",
                 "Accept-Encoding: identity", f"Content-Length: {len(body)}",
                 *(["Content-Type: application/octet-stream"] if method in ("POST", "PUT")
                   else []), *(f"{k}: {v}" for k, v in headers.items())]
        answer = self._stream(self._ns("/api/v1", "pods", pod) + "/portforward",
                              {"ports": str(port)}, ("\r\n".join(lines) + "\r\n\r\n").encode()
                              + body, timeout=timeout, limit=limit + 65536,
                              complete=_whole_answer)
        head, separator, rest = answer.partition(b"\r\n\r\n")
        if not separator:
            raise ContractError("Agent answer cut short")
        lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError) as exc:
            raise ContractError("Unexpected agent answer") from exc
        fields = {n.strip().lower(): v.strip() for n, _, v in
                  (line.partition(":") for line in lines[1:])}
        if status != 200:
            raise KubernetesRequestRefused(status, refusal_reason(rest))
        if "content-length" not in fields or fields.get("transfer-encoding"):
            raise ContractError("Agent answer without a length")
        length = int(fields["content-length"])
        if len(rest) < length or length > limit:
            raise ContractError("Agent answer cut short")
        return rest[:length]

    def proxy(self, method, pod, port, path, query, *, headers, body=None, timeout=60,
              limit=1 << 20):
        """A request to ``pod:port`` through the API server, answered as bytes."""
        if type(port) is not int or not 1 <= port <= 65535 or type(path) is not str \
                or not path.startswith("/") or ".." in path:
            raise ContractError("Exact agent request required")
        return self._request(
            method, self._ns("/api/v1", "pods", pod) + f":{port}/proxy{path}", query,
            timeout=timeout, body=body,
            content_type="application/octet-stream" if method in ("POST", "PUT") else None,
            limit=limit, raw=True, headers=headers, transfer=True)
