"""Whether a cluster's Service dataplane keeps a connection already
established through a Service flowing once the Service's selector moves to a
side without Pods.

A Service route's pause relies on it: new connections are refused, the open
sessions run to their end. kube-proxy's connection tracking keeps such flows;
GKE Dataplane V2 (eBPF) drops them (measured: three open matches timed out
seven seconds after the selector moved, and the server never saw them end).
So it is measured, not assumed, in the unit's namespace with Pods and a
Service of its own, before anything of the unit changes:

1. a server Pod that echoes lines, behind a Service selecting it;
2. a client Pod that opens one connection to the Service and exchanges a line
   every half second, writing each round trip to its log;
3. once the exchange runs, the Service's selector moves to a side without
   Pods - exactly what the pause does - and the endpoints are seen gone;
4. the exchange must go on for ``observe_seconds`` without an error.

The Pods run as the given non-root user from the given pinned image, with
nothing mounted, and are removed with the Service whatever happens.
"""

import json
import secrets
import time

from sova.domain.contracts import ContractError

_SERVER = r'''
import socketserver, sys
class Echo(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            self.wfile.write(line)
            self.wfile.flush()
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
Server(("", int(sys.argv[1])), Echo).serve_forever()
'''

_CLIENT = r'''
import json, socket, sys, time
host, port, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
start, exchanged = time.monotonic(), 0
try:
    while True:  # a new Service is answered once the dataplane has its rules
        try:
            connection = socket.create_connection((host, port), timeout=5)
            break
        except OSError:
            if time.monotonic() - start > 30:
                raise
            time.sleep(0.5)
    start = time.monotonic()
    connection.settimeout(3)
    stream = connection.makefile("rwb")
    while time.monotonic() - start < seconds:
        stream.write(b"ping\n")
        stream.flush()
        if stream.readline() != b"ping\n":
            raise ValueError("unexpected answer")
        exchanged += 1
        print(json.dumps({"exchanged": exchanged}), flush=True)
        time.sleep(0.5)
    print(json.dumps({"exchanged": exchanged, "done": True}), flush=True)
except Exception as error:
    print(json.dumps({"exchanged": exchanged, "error": type(error).__name__}), flush=True)
time.sleep(seconds)  # the last line stays readable until the Pod is removed
'''

PORT = 8080


class ServiceFlowCheck:
    def __init__(self, controls, *, image, run_as, label, observe_seconds=8.0,
                 start_seconds=120.0, settle_seconds=30.0):
        if type(label) is not str or not label.isalnum() or not 1 <= len(label) <= 12:
            raise ContractError("A short check label required")
        self.controls, self.image, self.run_as = controls, image, run_as
        self.label, self.observe_seconds = label.lower(), float(observe_seconds)
        self.start_seconds, self.settle_seconds = float(start_seconds), float(settle_seconds)

    def _pod(self, name, role, command):
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": name, "labels": {"app.kubernetes.io/managed-by": "sova",
                                     "sova.dev/flow-check": self.tag, "sova.dev/flow-role": role}},
            "spec": {
                "restartPolicy": "Never", "activeDeadlineSeconds": 300,
                "terminationGracePeriodSeconds": 0, "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": self.run_as[0],
                                    "runAsGroup": self.run_as[1],
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": role, "image": self.image, "imagePullPolicy": "IfNotPresent",
                    "command": ["python3", "-I", "-c", *command],
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                  "limits": {"memory": "128Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}}}]}}

    def _running(self, name):
        found = self.controls.pod(name) or {}
        statuses = (found.get("status") or {}).get("containerStatuses") or ()
        return any("running" in (s.get("state") or {}) for s in statuses)

    def _last(self, name, role):
        try:
            line = self.controls.container_log(name, role).strip().splitlines()[-1]
            return json.loads(line)
        except (ValueError, IndexError, ContractError):
            return {}

    def _wait(self, predicate, seconds, step=0.5):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)
        return True

    def measure(self):
        """(flows survive, detail). An answer that cannot be reached - the Pods
        not starting, no exchange before the move - is an error, not a verdict."""
        self.tag = f"{self.label}{secrets.token_hex(3)}"
        names = {role: f"sova-flow-{self.tag}-{role}" for role in ("server", "client")}
        service = f"sova-flow-{self.tag}"
        on = {"sova.dev/flow-check": self.tag, "sova.dev/flow-role": "server"}
        off = {"sova.dev/flow-check": self.tag, "sova.dev/flow-role": "nobody"}
        try:
            self.controls.create_pod(self._pod(names["server"], "server",
                                               [_SERVER, str(PORT)]))
            created = self.controls.create_check_service(
                service, selector=on, port=PORT,
                labels={"app.kubernetes.io/managed-by": "sova", "sova.dev/flow-check": self.tag})
            address = (created.get("spec") or {}).get("clusterIP")
            if not address or address == "None":
                raise ContractError("The check Service got no cluster address")
            if not self._wait(lambda: any(ready for _, ready in
                                          self.controls.endpoints(service)), self.start_seconds):
                raise ContractError("The check server did not become an endpoint in time")
            self.controls.create_pod(self._pod(
                names["client"], "client",
                [_CLIENT, address, str(PORT), str(self.observe_seconds + 60)]))
            if not self._wait(lambda: self._last(names["client"], "client").get(
                    "exchanged", 0) >= 3 or "error" in self._last(names["client"], "client"),
                    self.start_seconds):
                raise ContractError("The check client did not start exchanging in time")
            before = self._last(names["client"], "client")
            if "error" in before:
                raise ContractError(f"The check connection failed before the move: "
                                    f"{before['error']}")
            current = self.controls.service(service)
            self.controls.set_service_selector(service, current["metadata"]["resourceVersion"],
                                               off)
            if not self._wait(lambda: not self.controls.endpoints(service),
                              self.settle_seconds):
                raise ContractError("The check Service kept its endpoints after the move")
            time.sleep(self.observe_seconds)
            after = self._last(names["client"], "client")
            exchanged = max(after.get("exchanged", 0) - before["exchanged"], 0)
            # At one exchange every half second, most of them must have happened.
            survive = "error" not in after and exchanged >= int(self.observe_seconds)
            return survive, {"measured": "service_selector_moved_with_open_connection",
                             "observed_seconds": self.observe_seconds,
                             "exchanges_after_move": exchanged,
                             "client_error": after.get("error")}
        finally:
            for name in names.values():
                self.controls.delete_pod(name)
            self.controls.delete_service(service)
