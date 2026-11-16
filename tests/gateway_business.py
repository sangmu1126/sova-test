"""An application behind a Gateway API entry point, on the disposable kind
cluster with Envoy Gateway (``test-env.sh gateway``): two sides of a web
service, each answering with its side marker, one Gateway and one HTTPRoute
sending everything to the source. The code under test holds only a token that
may read and patch HTTPRoutes in this namespace.

Requests through the Gateway go from the test, through the API server's
Service proxy, to the Envoy proxy the Gateway runs: the route really is what
Envoy serves, not what the HTTPRoute says.
"""

import json
import os
import time

import pytest
from volume_business import PYTHON, security

SERVER = r'''
import http.server, json, os
SIDE = os.environ["SIDE"]
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        body = json.dumps({"side": SIDE}).encode()
        self.send_response(200)
        self.send_header("X-Sova-Side", SIDE)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.ThreadingHTTPServer(("", 8080), Handler).serve_forever()
'''

RULES = [{"apiGroups": ["gateway.networking.k8s.io"], "resources": ["httproutes"],
          "verbs": ["get", "patch"]}]


def web(side):
    labels = {"app": "web", "side": side}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": f"web-{side}"},
         "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "terminationGracePeriodSeconds": 1, "securityContext": security(),
                      "containers": [{
                          "name": "web", "image": PYTHON, "imagePullPolicy": "IfNotPresent",
                          "command": ["python3", "-I", "-c", SERVER],
                          "env": [{"name": "SIDE", "value": side}],
                          "ports": [{"containerPort": 8080}],
                          "readinessProbe": {"httpGet": {"path": "/", "port": 8080},
                                             "periodSeconds": 1},
                          "resources": {"requests": {"cpu": "10m", "memory": "24Mi"}},
                          "securityContext": {"allowPrivilegeEscalation": False,
                                              "capabilities": {"drop": ["ALL"]}}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"web-{side}"},
         "spec": {"selector": labels, "ports": [{"port": 80, "targetPort": 8080}]}}]


def objects():
    return [*web("source"), *web("target"),
            {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
             "metadata": {"name": "web"},
             "spec": {"gatewayClassName": "sova-envoy", "listeners": [{
                 "name": "http", "protocol": "HTTP", "port": 80,
                 "allowedRoutes": {"namespaces": {"from": "Same"}}}]}},
            {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
             "metadata": {"name": "web"},
             "spec": {"parentRefs": [{"name": "web"}], "rules": [{
                 "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                 "backendRefs": [{"name": "web-source", "port": 80}]}]}}]


@pytest.fixture
def gateway_business():
    from kind_support import delete_namespace, kind_namespace, kubectl

    context = os.environ.get("SOVA_TEST_KUBE_CONTEXT")
    if context:
        try:
            kubectl(context, "get", "gatewayclass", "sova-envoy")
        except AssertionError:
            pytest.skip("Envoy Gateway is not installed: run infra/test-env/test-env.sh gateway")
    context, namespace, controls = kind_namespace(RULES, objects(), prefix="gateway")

    def run(*args, stdin=None):
        return kubectl(context, "-n", namespace, *args, stdin=stdin)

    try:
        for side in ("source", "target"):
            run("rollout", "status", f"deployment/web-{side}", "--timeout=180s")
        run("wait", "--for=condition=Programmed", "gateway/web", "--timeout=180s")
        selector = ("gateway.envoyproxy.io/owning-gateway-name=web,"
                    f"gateway.envoyproxy.io/owning-gateway-namespace={namespace}")
        envoy = kubectl(context, "-n", "envoy-gateway-system", "get", "service", "-l", selector,
                        "-o", "jsonpath={.items[0].metadata.name}").strip()

        def observe():
            """One request through the Envoy proxy: the side that answered."""
            raw = kubectl(context, "get", "--raw",
                          f"/api/v1/namespaces/envoy-gateway-system/services/{envoy}:80/proxy/")
            return {"source": "web_src", "target": "web_tgt"}[json.loads(raw)["side"]]

        deadline = time.monotonic() + 120  # Envoy is programmed asynchronously
        while time.monotonic() < deadline:
            try:
                if observe() == "web_src":
                    break
            except (AssertionError, ValueError, KeyError):
                pass
            time.sleep(1)
        yield {"context": context, "namespace": namespace, "controls": controls, "run": run,
               "observe": observe}
    finally:
        delete_namespace(context, namespace)
