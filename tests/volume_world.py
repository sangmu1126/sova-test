"""Kubernetes volume handoff world for the provider-neutral conformance kit.

Observed independently of the adapter: content is a digest of every entry
under the claim (path, kind, mode, owner, size, SHA-256 or link target),
computed by an observer Pod of the test's own that mounts the claim read-only;
a write attempt is a real write through the writer's own HTTP endpoint (a
SQLite transaction rolled back and a file removed again); the write path that
outlives every registered writer is a Pod someone started by hand with the
claim mounted read-write.
"""

import json
import secrets
import time

from volume_business import PYTHON, SIDES, UID, bare_pod

from sova.adapters.kubernetes.handoff_controls import KubernetesServiceRoute
from sova.adapters.kubernetes_volume_handoff import KubernetesVolumeHandoffAdapter, VolumeSide

DIGEST = r'''
import hashlib, json, os, stat
out = []
def walk(directory, prefix):
    for name in sorted(os.listdir(directory)):
        path, full = prefix + name, os.path.join(directory, name)
        info = os.lstat(full)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            out.append([path, "l", 0, info.st_uid, info.st_gid, 0, os.readlink(full)])
        elif stat.S_ISDIR(info.st_mode):
            out.append([path, "d", mode, info.st_uid, info.st_gid, 0, ""])
            walk(full, path + "/")
        else:
            with open(full, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            out.append([path, "f", mode, info.st_uid, info.st_gid, info.st_size, digest])
walk("/data", "")
print(hashlib.sha256(json.dumps(out).encode()).hexdigest())
'''


class Route(KubernetesServiceRoute):
    pass


class VolumeWorld:
    KEY = secrets.token_bytes(32)

    def __init__(self, business):
        self.b, self.run, self.post = business, business["run"], business["post"]
        self.controls = business["controls"]()
        self._route = Route(self.controls, "board", SIDES, converge_seconds=60)
        self.pending = None
        self.writers = {"app": "board", "other": "board-worker"}

    @staticmethod
    def _claim(side):
        return f"board-data-{'source' if side == 'source' else 'target'}"

    def _side(self, side, controls):
        name = "source" if side == "source" else "target"
        return VolumeSide(controls, (f"board-data-{name}",), ("deployment", f"board-{name}"),
                          (("deployment", f"board-worker-{name}"),))

    def _adapter(self, source, target, routes):
        controls = self.b["controls"]()
        return KubernetesVolumeHandoffAdapter(
            "kubernetes-volume-handoff", source=self._side(source, controls),
            target=self._side(target, controls), route=Route(controls, "board", SIDES,
                                                             converge_seconds=60),
            routes=routes, agent_key=self.KEY, image=PYTHON, run_as=(UID, UID),
            storage_class="standard", settle_seconds=90, start_seconds=180)

    def adapter(self):
        return self._adapter("source", "target", ("board_src", "board_tgt"))

    def reverse_adapter(self):
        return self._adapter("target", "source", ("board_tgt", "board_src"))

    # -- observation -------------------------------------------------------------
    def _json(self, *args):
        return json.loads(self.run(*args, "-o", "json"))

    def _pods(self):
        return self._json("get", "pods")["items"]

    def _bound(self, claim):
        found = [c for c in self._json("get", "pvc")["items"] if c["metadata"]["name"] == claim]
        return bool(found) and found[0]["status"].get("phase") == "Bound"

    def _observer(self, side):
        name, claim = f"observer-{side}", self._claim(side)
        pods = {p["metadata"]["name"]: p for p in self._pods()}
        if name not in pods:
            self.run("apply", "-f", "-", stdin=json.dumps(bare_pod(
                name, claim, ["python3", "-c", "import time; time.sleep(86400)"],
                read_only=True)))
            self.run("wait", "--for=condition=Ready", f"pod/{name}", "--timeout=120s")
        return name

    def content(self, side):
        if not self._bound(self._claim(side)):
            return None
        return self.run("exec", self._observer(side), "--", "python3", "-I", "-c",
                        DIGEST).strip()

    def _writing(self, side):
        claim = self._claim(side)
        out = []
        for pod in self._pods():
            if pod["status"].get("phase") in ("Succeeded", "Failed"):
                continue
            volumes = {v["name"]: v["persistentVolumeClaim"] for v in pod["spec"]["volumes"]
                       if (v.get("persistentVolumeClaim") or {}).get("claimName") == claim}
            if any(m["name"] in volumes and not m.get("readOnly")
                   and not volumes[m["name"]].get("readOnly")
                   for c in pod["spec"]["containers"] for m in c.get("volumeMounts") or ()):
                out.append(pod["metadata"]["name"])
        return sorted(out)

    def _workloads(self):
        return sorted((d["metadata"]["name"], d["spec"]["replicas"])
                      for d in self._json("get", "deployments")["items"])

    def state(self):
        return (self.content("source"), self.content("target"), self._workloads(),
                self._writing("source"), self._writing("target"), self._route.get())

    # -- the business and writers -----------------------------------------------------
    def _serving(self, side, app):
        label = f"app={app},side={'source' if side == 'source' else 'target'}"
        pods = self._json("get", "pods", "-l", label)["items"]
        return [p["metadata"]["name"] for p in pods if not p["metadata"].get("deletionTimestamp")
                and any(c["type"] == "Ready" and c["status"] == "True"
                        for c in p["status"].get("conditions") or ())]

    def can_write(self, side, writer):
        for pod in self._serving(side, self.writers[writer]):
            try:
                if self.post(pod, "/probe").get("written"):
                    return True
            except (AssertionError, ValueError):
                continue  # gone or refusing: this one cannot write
        return False

    def business_write(self, count):
        self.post(self._serving("source", "board")[0], f"/posts?count={count}")

    def business_delete(self, count):
        self.post(self._serving("source", "board")[0], f"/delete?count={count}")

    def posts(self, side):
        pod = self._serving(side, "board")[0]
        namespace = self.b["namespace"]
        return json.loads(self.run("get", "--raw",
                                   f"/api/v1/namespaces/{namespace}/pods/{pod}:8080/proxy/posts"))

    # -- changes outside the plan ----------------------------------------------------------
    def reopen_source(self):
        """Someone starts the application on the source again."""
        self.run("scale", "deployment/board-source", "--replicas=1")
        self.run("rollout", "status", "deployment/board-source", "--timeout=180s")
        deadline = time.monotonic() + 60
        while not self._serving("source", "board") and time.monotonic() < deadline:
            time.sleep(0.5)

    def tamper_target(self):
        name = "tamper"
        self.run("apply", "-f", "-", stdin=json.dumps(bare_pod(
            name, self._claim("target"), ["python3", "-c",
                                          "open('/data/uploads/forged.txt','w').write('x')"],
            read_only=False)))
        self.run("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{name}",
                 "--timeout=120s")
        self.run("delete", "pod", name, "--wait=true")

    def move_route(self, side):
        self.run("patch", "service", "board", "--type=merge", "-p", json.dumps(
            {"spec": {"selector": SIDES[self.route_of(side)]}}))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return "board_src" if side == "source" else "board_tgt"

    def open_pending_write(self):
        """A Pod started by hand with the source claim mounted read-write."""
        self.pending = "maintenance"
        self.run("apply", "-f", "-", stdin=json.dumps(bare_pod(
            self.pending, self._claim("source"),
            ["python3", "-c", "import time; time.sleep(86400)"], read_only=False)))
        self.run("wait", "--for=condition=Ready", f"pod/{self.pending}", "--timeout=120s")

    def resolve_pending_write(self):
        self.run("delete", "pod", self.pending, "--grace-period=0", "--wait=true")
