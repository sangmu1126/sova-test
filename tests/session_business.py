"""A WebSocket server that keeps its sessions in memory, on a disposable kind
cluster: the shape of a real-time game server (and of the demo's match
server). Source and target Deployments in one namespace behind one Service;
the target is created stopped, and a third, "paused" side matches no Pod.

Sessions are opened by client Pods inside the cluster, through the Service, so
the route really is what kube-proxy does: each client writes one JSON line per
session (which Pod served it, whether it ended normally) and a summary.
"""

import json
import time

import pytest
from volume_business import PYTHON, security

SIDES = {"game_src": {"app": "game", "side": "source"},
         "game_tgt": {"app": "game", "side": "target"},
         "game_paused": {"app": "game", "side": "paused"}}

SERVER = r'''
import base64, hashlib, json, os, socketserver, struct

POD = os.environ.get("POD_NAME", "unknown")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def read_frame(sock):
    first, second = exact(sock, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(sock, 8))[0]
    mask = exact(sock, 4) if second & 0x80 else b"\0\0\0\0"
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(sock, length)))
    return first & 0x0F, payload


def send_frame(sock, opcode, payload):
    head = bytes([0x80 | opcode])
    if len(payload) < 126:
        head += bytes([len(payload)])
    else:
        head += bytes([126]) + struct.pack("!H", len(payload))
    sock.sendall(head + payload)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            request += chunk
        lines = request.split(b"\r\n\r\n")[0].decode().split("\r\n")
        headers = {k.strip().lower(): v.strip()
                   for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        if lines[0].split()[1] == "/healthz":
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                                 b"Connection: close\r\n\r\nok")
            return
        accept = base64.b64encode(hashlib.sha1(
            (headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
        self.request.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept +
                              "\r\n\r\n").encode())
        send_frame(self.request, 1, json.dumps({"hello": POD}).encode())
        while True:
            opcode, payload = read_frame(self.request)
            if opcode == 8:
                send_frame(self.request, 8, payload[:2])
                return
            if opcode == 1:  # the match state lives here, in this process only
                send_frame(self.request, 1, json.dumps(
                    {"pod": POD, "echo": payload.decode()}).encode())


socketserver.ThreadingTCPServer.allow_reuse_address = True
server = socketserver.ThreadingTCPServer(("", 8080), Handler)
server.daemon_threads = True
server.serve_forever()
'''

CLIENT = r'''
import base64, json, os, socket, struct, sys, threading, time

HOST, PLAN = sys.argv[1], json.loads(sys.argv[2])


def exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed by server")
        data += chunk
    return data


def send(sock, opcode, payload):
    mask = os.urandom(4)
    head = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
    sock.sendall(head + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def receive(sock):
    first, second = exact(sock, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", exact(sock, 2))[0]
    return first & 0x0F, exact(sock, length)


def session(entry, out):
    time.sleep(entry.get("start", 0))
    result = {"id": entry["id"], "connected": False, "completed": False, "pod": None}
    try:
        sock = socket.create_connection((HOST, 80), timeout=5)
    except OSError as error:
        result["error"] = type(error).__name__
        out.append(result)
        return
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /match HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += exact(sock, 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("no upgrade")
        result["connected"] = True
        result["pod"] = json.loads(receive(sock)[1])["hello"]
        deadline, moves = time.monotonic() + entry["seconds"], 0
        while time.monotonic() < deadline:
            send(sock, 1, f"move {moves}".encode())
            reply = json.loads(receive(sock)[1])
            if reply["pod"] != result["pod"]:
                raise ConnectionError("answered by another server")
            moves += 1
            time.sleep(0.5)
        send(sock, 8, struct.pack("!H", 1000))
        receive(sock)
        result.update(completed=True, moves=moves)
    except (OSError, ValueError, KeyError) as error:
        result["error"] = f"{type(error).__name__}: {error}"[:120]
    finally:
        sock.close()
    out.append(result)


results = []
threads = [threading.Thread(target=session, args=(e, results)) for e in PLAN]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(json.dumps({"sessions": sorted(results, key=lambda r: r["id"])}), flush=True)
'''

RULES = [
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["pods/ephemeralcontainers"], "verbs": ["patch"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "patch"]},
    {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "patch"]},
]


def server(name, side, *, replicas):
    labels = {"app": "game", "side": side}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "terminationGracePeriodSeconds": 1, "securityContext": security(),
                         "containers": [{
                             "name": "server", "image": PYTHON, "imagePullPolicy": "IfNotPresent",
                             "command": ["python3", "-I", "-c", SERVER],
                             "env": [{"name": "POD_NAME", "valueFrom": {
                                 "fieldRef": {"fieldPath": "metadata.name"}}}],
                             "ports": [{"containerPort": 8080}],
                             "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080},
                                                "periodSeconds": 1},
                             "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                             "securityContext": {"allowPrivilegeEscalation": False,
                                                 "capabilities": {"drop": ["ALL"]}}}]}}}}


def objects():
    return [server("game-source", "source", replicas=2),
            server("game-target", "target", replicas=0),
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "game"},
             "spec": {"selector": SIDES["game_src"],
                      "ports": [{"port": 80, "targetPort": 8080}]}}]


@pytest.fixture
def session_business():
    from kind_support import delete_namespace, kind_namespace, kubectl

    from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls

    context, namespace, controls = kind_namespace(
        RULES, objects(), prefix="session", controls_class=KubernetesVolumeControls)

    def run(*args, stdin=None):
        return kubectl(context, "-n", namespace, *args, stdin=stdin)

    def clients(name, plan):
        """Start a client Pod opening ``plan``'s sessions through the Service."""
        run("apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name},
            "spec": {"restartPolicy": "Never", "securityContext": security(),
                     "containers": [{
                         "name": "client", "image": PYTHON, "imagePullPolicy": "IfNotPresent",
                         "command": ["python3", "-I", "-c", CLIENT,
                                     f"game.{namespace}.svc.cluster.local", json.dumps(plan)],
                         "securityContext": {"allowPrivilegeEscalation": False,
                                             "capabilities": {"drop": ["ALL"]}}}]}}))

    def results(name, timeout=300):
        run("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{name}",
            f"--timeout={timeout}s")
        return {s["id"]: s for s in json.loads(run("logs", name).strip())["sessions"]}

    def started(name):
        """Wait until a client Pod runs (its sessions are being opened)."""
        run("wait", "--for=condition=Ready", f"pod/{name}", "--timeout=120s")
        time.sleep(2)

    try:
        run("rollout", "status", "deployment/game-source", "--timeout=180s")
        yield {"context": context, "namespace": namespace, "controls": controls, "run": run,
               "clients": clients, "results": results, "started": started}
    finally:
        delete_namespace(context, namespace)
