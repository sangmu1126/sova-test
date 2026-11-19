"""Whether a workload outside the application can open a connection to the
application's data endpoint on a Kubernetes target (NET-01.3).

A Service's address is reachable from every namespace unless a
NetworkPolicy - enforced by the cluster's network plugin - says otherwise;
writing the policy is not the question, the plugin's enforcement is. So it is
measured, with Pods of the probe's own:

1. positive control - a client Pod in the application's namespace, carrying
   the labels the application's clients carry, connects to the endpoint;
2. for each registered outside namespace, a client Pod there (no labels of
   the application's) tries the same endpoint and must not connect;
3. the positive control again.

A client Pod makes one TCP connection with a timeout and writes one JSON line:
connected, refused (reset: nothing accepted it) or timed out (dropped).
Refused and timed out both count as no reach; a Pod that does not start or
answer is undecided, never a refusal. The Pods run as the given non-root
user from the given pinned image, mount nothing, carry no service account
token, and are removed whatever happens. The endpoint is named as the
cluster's DNS resolves it (``<service>.<namespace>.svc``) and a port.
"""

import json
import re
import secrets
import time

from sova.domain.contracts import ContractError

_CLIENT = r'''
import json, socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    socket.create_connection((host, port), timeout=timeout).close()
    found = "connected"
except ConnectionRefusedError:
    found = "refused"
except (TimeoutError, socket.timeout):
    found = "timed_out"
except OSError as error:
    found = "error:" + type(error).__name__
print(json.dumps({"reach": found}), flush=True)
import time
time.sleep(600)  # readable until the Pod is removed
'''
_HOST = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
NO_REACH = frozenset({"refused", "timed_out"})


class NetworkReachCheck:
    def __init__(self, *, inside, outside, host, port, client_labels, image, run_as,
                 connect_seconds=5.0, start_seconds=120.0):
        """``inside`` is the controls of the application's namespace,
        ``outside`` a list of ``(name, controls)`` of other namespaces; each
        controls creates, reads and deletes Pods in its own namespace."""
        if type(host) is not str or not _HOST.fullmatch(host):
            raise ContractError("The endpoint's DNS name required")
        if type(port) is not int or not 0 < port < 65536:
            raise ContractError("The endpoint's port required")
        if not outside or not all(type(o) is tuple and len(o) == 2 for o in outside):
            raise ContractError("At least one outside namespace, as (name, controls)")
        if type(client_labels) is not dict or not client_labels:
            raise ContractError("The labels of the application's clients required")
        self.inside, self.outside = inside, tuple(outside)
        self.host, self.port, self.labels = host, port, dict(client_labels)
        self.image, self.run_as = image, tuple(run_as)
        self.connect_seconds, self.start_seconds = float(connect_seconds), float(start_seconds)

    def _pod(self, name, labels):
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": name, "labels": {**labels, "app.kubernetes.io/managed-by": "sova",
                                     "sova.dev/reach-check": name}},
            "spec": {
                "restartPolicy": "Never", "activeDeadlineSeconds": 300,
                "terminationGracePeriodSeconds": 0, "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": self.run_as[0],
                                    "runAsGroup": self.run_as[1],
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "client", "image": self.image, "imagePullPolicy": "IfNotPresent",
                    "command": ["python3", "-I", "-c", _CLIENT, self.host, str(self.port),
                                str(self.connect_seconds)],
                    "resources": {"requests": {"cpu": "20m", "memory": "32Mi"},
                                  "limits": {"memory": "64Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}}}]}}

    def _reach(self, controls, labels):
        """One client's outcome: connected, refused, timed_out, or undecided."""
        name = f"sova-reach-{secrets.token_hex(4)}"
        try:
            controls.create_pod(self._pod(name, labels))
            deadline = time.monotonic() + self.start_seconds
            while time.monotonic() < deadline:
                try:
                    line = controls.container_log(name, "client").strip().splitlines()
                    if line:
                        return json.loads(line[-1]).get("reach", "undecided:no_answer")
                except (ContractError, ValueError, IndexError):
                    pass
                found = controls.pod(name) or {}
                if (found.get("status") or {}).get("phase") in ("Failed", "Succeeded"):
                    return "undecided:client_ended"
                time.sleep(1)
            return "undecided:client_did_not_answer"
        finally:
            controls.delete_pod(name)

    def measure(self):
        """(passed, observation)."""
        before = self._reach(self.inside, self.labels)
        outside = {name: self._reach(controls, {}) for name, controls in self.outside}
        after = self._reach(self.inside, self.labels)
        if any(found == "connected" for found in outside.values()):
            outcome = "reached"
        elif before == after == "connected" and all(f in NO_REACH for f in outside.values()):
            outcome = "denied"
        else:
            outcome = "undecided"
        return outcome == "denied", {
            "probe": "cross_namespace_reach_denied", "outcome": outcome,
            "endpoint": f"{self.host}:{self.port}",
            "positive": {"before": before, "after": after}, "outside": outside}
