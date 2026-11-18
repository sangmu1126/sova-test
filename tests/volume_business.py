"""A real file-state application on a disposable kind cluster, for volume handoffs.

The shape of a common small web application (and of the demo board): a SQLite
database in WAL mode and an upload folder on one PersistentVolumeClaim,
written by the application and by a second writer (a background worker), with
a Service in front. Both sides live in one namespace: the source writers run
on ``board-data-source``, the target writers are created stopped and mount
``board-data-target``, which does not exist yet (staging creates it).

The application is a standard-library HTTP server in the same pinned Python
image the adapter's agents use, so nothing is built or pulled from elsewhere.
Each test gets its own namespace with a token limited to what the adapter
needs; ``SOVA_TEST_KUBE_CONTEXT`` names the kind context.
"""

import json
import time

import pytest

PYTHON = ("docker.io/library/python@sha256:"
          "5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b")
UID = 10001
SIDES = {"board_src": {"app": "board", "side": "source"},
         "board_tgt": {"app": "board", "side": "target"}}

APP = r'''
import http.server, json, os, sqlite3
from urllib.parse import parse_qs, urlsplit

DATA = "/data"
DB, UPLOADS = DATA + "/board.db", DATA + "/uploads"
ROLE = os.environ.get("ROLE", "app")


def db():
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    os.makedirs(UPLOADS, exist_ok=True)
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "title TEXT NOT NULL, attachment TEXT)")
    c.close()


def posts(count):
    c = db()
    c.execute("BEGIN IMMEDIATE")
    for _ in range(count):
        pid = c.execute("INSERT INTO posts(title) VALUES (?)", (f"post by {ROLE}",)).lastrowid
        name = f"{pid}.txt"
        with open(os.path.join(UPLOADS, name), "w") as f:
            f.write(f"attachment of post {pid}\n")
        c.execute("UPDATE posts SET attachment=? WHERE id=?", (name, pid))
    c.execute("COMMIT")
    c.close()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        if self.path == "/posts":
            c = db()
            rows = c.execute("SELECT id, title, attachment FROM posts ORDER BY id").fetchall()
            check = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.close()
            files = sorted(os.listdir(UPLOADS))
            return self.reply(200, {"posts": rows, "integrity": check, "uploads": files})
        self.reply(404, {})

    def do_POST(self):
        url = urlsplit(self.path)
        count = int(parse_qs(url.query).get("count", ["1"])[0])
        try:
            if url.path == "/probe":  # a real write, always undone
                c = db()
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT INTO posts(title) VALUES ('sova-probe')")
                c.execute("ROLLBACK")
                c.close()
                probe = os.path.join(UPLOADS, f".probe-{os.getpid()}")
                with open(probe, "w") as f:
                    f.write("x")
                os.unlink(probe)
                return self.reply(200, {"written": True, "role": ROLE})
            if url.path == "/posts":
                posts(count)
                return self.reply(200, {"written": count})
            if url.path == "/delete":
                c = db()
                c.execute("BEGIN IMMEDIATE")
                rows = c.execute("SELECT id, attachment FROM posts ORDER BY id LIMIT ?",
                                 (count,)).fetchall()
                for pid, attachment in rows:
                    c.execute("DELETE FROM posts WHERE id=?", (pid,))
                    if attachment:
                        os.unlink(os.path.join(UPLOADS, attachment))
                c.execute("COMMIT")
                c.close()
                return self.reply(200, {"deleted": len(rows)})
            if url.path == "/seed":
                os.makedirs(UPLOADS + "/2026/11", exist_ok=True)
                with open(UPLOADS + "/2026/11/photo.bin", "wb") as f:
                    f.write(os.urandom(300_000))
                os.makedirs(DATA + "/cache", exist_ok=True)
                with open(DATA + "/private.txt", "w") as f:
                    f.write("settings only the application reads\n")
                os.chmod(DATA + "/private.txt", 0o640)
                if not os.path.lexists(DATA + "/latest"):
                    os.symlink("uploads/2026/11/photo.bin", DATA + "/latest")
                posts(count)
                return self.reply(200, {"seeded": count})
        except Exception as error:
            return self.reply(500, {"error": repr(error)})
        self.reply(404, {})


init()
http.server.ThreadingHTTPServer(("", 8080), Handler).serve_forever()
'''

RULES = [
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "create", "delete"]},
    {"apiGroups": [""], "resources": ["pods/proxy"], "verbs": ["get", "create", "update"]},
    {"apiGroups": [""], "resources": ["pods/portforward"], "verbs": ["get", "create"]},
    {"apiGroups": [""], "resources": ["persistentvolumeclaims"],
     "verbs": ["get", "list", "create", "patch", "delete"]},
    {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "patch"]},
    {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]},
    {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets"],
     "verbs": ["get", "patch"]},
    {"apiGroups": ["batch"], "resources": ["cronjobs"], "verbs": ["get", "patch"]},
    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list"]},
]


def security():
    return {"runAsNonRoot": True, "runAsUser": UID, "runAsGroup": UID,
            "seccompProfile": {"type": "RuntimeDefault"}}


def container(name, command, *, mounts, env=()):
    return {"name": name, "image": PYTHON, "imagePullPolicy": "IfNotPresent",
            "command": command, "env": [{"name": k, "value": v} for k, v in env],
            "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": mounts}


def writer(name, app, side, *, replicas, role):
    labels = {"app": app, "side": side}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
                     "strategy": {"type": "Recreate"},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "terminationGracePeriodSeconds": 1, "securityContext": security(),
                         "containers": [{
                             **container("app", ["python3", "-I", "-c", APP],
                                         mounts=[{"name": "data", "mountPath": "/data"}],
                                         env=[("ROLE", role)]),
                             "ports": [{"containerPort": 8080}],
                             "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080},
                                                "periodSeconds": 1}}],
                         "volumes": [{"name": "data", "persistentVolumeClaim": {
                             "claimName": f"board-data-{side}"}}]}}}}


def objects():
    from kind_support import storage_class

    return [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "board-data-source"},
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class(),
                  "resources": {"requests": {"storage": "1Gi"}}}},
        writer("board-source", "board", "source", replicas=1, role="app"),
        writer("board-worker-source", "board-worker", "source", replicas=1, role="worker"),
        writer("board-target", "board", "target", replicas=0, role="app"),
        writer("board-worker-target", "board-worker", "target", replicas=0, role="worker"),
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "board"},
         "spec": {"selector": SIDES["board_src"], "ports": [{"port": 80, "targetPort": 8080}]}},
    ]


def bare_pod(name, claim, command, *, read_only, uid=UID):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "labels": {"role": "test-helper"}},
            "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 0,
                     "securityContext": {**security(), "runAsUser": uid, "runAsGroup": uid},
                     "containers": [container("helper", command, mounts=[{
                         "name": "data", "mountPath": "/data", "readOnly": read_only}])],
                     "volumes": [{"name": "data", "persistentVolumeClaim": {
                         "claimName": claim, "readOnly": read_only}}]}}


@pytest.fixture
def volume_business():
    from kind_support import delete_namespace, kind_namespace, kubectl

    from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls

    context, namespace, controls = kind_namespace(
        RULES, objects(), prefix="volume", controls_class=KubernetesVolumeControls)

    def run(*args, stdin=None):
        return kubectl(context, "-n", namespace, *args, stdin=stdin)

    def post(pod, path):
        return json.loads(run("create", "--raw",
                              f"/api/v1/namespaces/{namespace}/pods/{pod}:8080/proxy{path}",
                              "-f", "-", stdin=""))

    try:
        for name in ("board-source", "board-worker-source"):
            run("rollout", "status", f"deployment/{name}", "--timeout=180s")
        app = run("get", "pods", "-l", "app=board,side=source", "-o",
                  "jsonpath={.items[0].metadata.name}").strip()
        post(app, "/seed?count=20")
        yield {"context": context, "namespace": namespace, "controls": controls, "run": run,
               "post": post}
    finally:
        delete_namespace(context, namespace)
        deadline = time.monotonic() + 120  # leave no volume behind on the node
        while time.monotonic() < deadline and namespace in kubectl(
                context, "get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"):
            time.sleep(1)


@pytest.fixture
def other_tenant():
    """Another tenant's namespace with a bound claim, read with its own token."""
    from kind_support import delete_namespace, kind_namespace, kubectl, storage_class

    from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls

    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "records"},
             "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class(),
                      "resources": {"requests": {"storage": "1Gi"}}}}
    holder = bare_pod("holder", "records", ["python3", "-c", "import time; time.sleep(86400)"],
                      read_only=True)
    context, namespace, controls = kind_namespace(
        [{"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get"]}],
        [claim, holder], prefix="other", controls_class=KubernetesVolumeControls)
    try:
        kubectl(context, "-n", namespace, "wait", "--for=condition=Ready", "pod/holder",
                "--timeout=180s")
        yield controls()
    finally:
        delete_namespace(context, namespace)
