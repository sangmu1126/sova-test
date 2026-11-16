"""One-Pod containment of the deployed chat app, not a synthetic application.

Runs inside the registered personal access tool. Local operator authorization is
not T10 approval. Never changes the app template, Service, DB or credentials.
Each effect is journaled before dispatch; uncertain effects require readback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from datetime import UTC, datetime

LABEL = "sova.dev/response-run"


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(path)


def selected(selector, labels):
    if any(labels.get(k) != v for k, v in selector.get("matchLabels", {}).items()):
        return False
    for e in selector.get("matchExpressions", []):
        key, op, values = e["key"], e["operator"], e.get("values", [])
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
        if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise ValueError("unsupported_selector")
    return True


def ready(pod):
    return not pod["metadata"].get("deletionTimestamp") and any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


# Fixed probe: no credential, shell, request body, URL or application code injection.
PROBE = r'''
import json, sys, urllib.request, urllib.error, time
url="http://"+sys.argv[1]+":8080"+sys.argv[2]
t=time.monotonic()
try:
 r=urllib.request.urlopen(url,timeout=3)
 out={"status":r.status,"connected":True}
 r.close()
except urllib.error.HTTPError as e:out={"status":e.code,"connected":True}
except (OSError,TimeoutError,urllib.error.URLError):out={"status":None,"connected":False}
print(json.dumps(out|{"elapsed_ms":round((time.monotonic()-t)*1000)}))
'''


class Runner:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise ValueError("private_owner_directory_required")
        self.db = sqlite3.connect(self.root / "effects.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS effects (name TEXT PRIMARY KEY, state TEXT NOT NULL, at TEXT NOT NULL)")
        self.db.commit()
        self.events = []

    def kube(self, *args, data=None):
        if not os.environ.get("KUBECONFIG"):
            raise ValueError("registered_access_wrapper_required")
        result = subprocess.run(["kubectl", "--request-timeout=15s", *args],
            input=json.dumps(data) if data is not None else None,
            text=True, capture_output=True, timeout=40)
        if result.returncode:
            raise RuntimeError("kubernetes_operation_failed_inspect_private_receipt")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def get(self, kind, name=None):
        args = ["-n", self.ns, "get", kind]
        if name:
            args.append(name)
        return self.kube(*args, "-o", "json")

    def event(self, stage, detail, **proof):
        self.events.append({"stage": stage, "at": now(), "detail": detail, "proof": proof})
        save(self.root / "receipt.json", {"kind": "actual-chat-response.v1", "application": "demo-chat",
            "namespace": self.ns, "plan_digest": digest(self.plan), "subject": self.plan["subject"],
            "observed_at": now(), "events": self.events, "scope": "operator_single_pod_drill",
            "product_approval": False, "migration_complete": False})
        print(json.dumps({"stage": stage, "detail": detail}, ensure_ascii=False), flush=True)

    def effect(self, name, call):
        # One attempt only, including after process restart or lost API response.
        self.db.execute("INSERT INTO effects VALUES (?, 'dispatched', ?)", (name, now()))
        self.db.commit()
        result = call()
        self.db.execute("UPDATE effects SET state='observed' WHERE name=?", (name,))
        self.db.commit()
        return result

    def guard(self):
        p = self.plan
        if datetime.now(UTC).timestamp() >= p["expires_epoch"]:
            raise ValueError("execution_window_expired")
        if self.kube("get", "ns", "kube-system", "-o", "json")["metadata"]["uid"] != p["cluster_uid"]:
            raise ValueError("wrong_cluster")
        dep = self.get("deployment", "demo-chat")
        if dep["metadata"]["uid"] != p["deployment_uid"] or digest(dep["spec"]) != p["deployment_spec_digest"]:
            raise ValueError("deployment_changed")
        service = self.get("service", "demo-chat")
        if service["metadata"]["uid"] != p["service_uid"] or digest(service["spec"]) != p["service_spec_digest"]:
            raise ValueError("service_changed")
        peer = self.get("pod", p["peer"]["name"])
        if peer["metadata"]["uid"] != p["peer"]["uid"] or not ready(peer):
            raise ValueError("independent_peer_changed")
        return dep

    def probe(self, actor, target, path="/healthz"):
        pod = self.get("pod", actor["name"])
        if pod["metadata"]["uid"] != actor["uid"]:
            raise ValueError("probe_actor_replaced")
        # Exec channel is only a fixed HTTP probe, never an operator shell.
        return self.kube("-n", self.ns, "exec", actor["name"], "-c", "app", "--", "python", "-c", PROBE, target["ip"], path)

    def prepare(self, cluster_uid):
        if (self.root / "plan.json").exists():
            raise ValueError("new_run_directory_required")
        self.ns = "target-dev"
        actual = self.kube("get", "ns", "kube-system", "-o", "json")["metadata"]["uid"]
        if actual != cluster_uid:
            raise ValueError("registered_cluster_mismatch")
        dep = self.get("deployment", "demo-chat")
        service = self.get("service", "demo-chat")
        selector = dep["spec"]["selector"]
        pods = [p for p in self.get("pods")["items"] if selected(selector, p["metadata"].get("labels", {}))]
        if len(pods) != dep["spec"]["replicas"] or len(pods) < 2 or not all(ready(p) for p in pods):
            raise ValueError("stable_multiple_replicas_required")
        rs = {r["metadata"]["uid"]: r for r in self.get("replicasets")["items"]}
        for pod in pods:
            owner = [o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller")]
            if len(owner) != 1 or owner[0]["kind"] != "ReplicaSet" or owner[0]["uid"] not in rs:
                raise ValueError("pod_owner_unverified")
            if not any(o.get("controller") and o["uid"] == dep["metadata"]["uid"] for o in rs[owner[0]["uid"]]["metadata"].get("ownerReferences", [])):
                raise ValueError("deployment_owner_unverified")
            if pod["spec"].get("hostNetwork") or LABEL in pod["metadata"].get("labels", {}):
                raise ValueError("unsupported_or_busy_pod")
            images = pod.get("status", {}).get("containerStatuses", [])
            if len(images) != 1 or images[0]["name"] != "app" or "sha256:" not in images[0].get("imageID", ""):
                raise ValueError("resolved_single_app_image_required")
        pods.sort(key=lambda p:p["metadata"]["name"])
        def ref(p):
            return {"name":p["metadata"]["name"],"uid":p["metadata"]["uid"],"ip":p["status"]["podIP"],"image_id":p["status"]["containerStatuses"][0]["imageID"]}
        subject, peer = ref(pods[0]), ref(pods[1])
        policies = self.get("networkpolicies")["items"]
        if any(selected(p["spec"]["podSelector"], pods[0]["metadata"]["labels"]) for p in policies):
            raise ValueError("existing_policy_requires_separate_review")
        self.plan = {"cluster_uid":actual,"namespace":self.ns,"deployment_uid":dep["metadata"]["uid"],
            "deployment_spec_digest":digest(dep["spec"]),"service_uid":service["metadata"]["uid"],
            "service_spec_digest":digest(service["spec"]),"subject":subject,"peer":peer,
            "original_pods":[ref(p) for p in pods],"expires_epoch":int(time.time())+1800,
            "policy_name":"sova-chat-"+os.urandom(6).hex(),"actions":["unauthenticated_admin_read","single_pod_network_isolation","uid_bound_pod_termination","controller_replacement_readback"]}
        self.guard()
        for actor, target in [(peer,subject),(subject,peer),(peer,peer)]:
            if self.probe(actor,target)["status"] != 200:
                raise ValueError("baseline_http_failed")
        save(self.root / "plan.json", self.plan)
        self.event("prepared", "실제 채팅 앱 선택 Pod·이웃 Pod의 HTTP 응답 확인", deployment_replicas=len(pods))
        print("PLAN_DIGEST="+digest(self.plan), flush=True)

    def run(self, approved_digest):
        self.plan = json.loads((self.root / "plan.json").read_text())
        self.ns = self.plan["namespace"]
        self.events = json.loads((self.root / "receipt.json").read_text())["events"]
        if self.ns != "target-dev" or digest(self.plan) != approved_digest:
            raise ValueError("exact_operator_plan_required")
        self.guard()
        subject,peer = self.plan["subject"],self.plan["peer"]
        outcome = self.effect("access_attempt", lambda:self.probe(peer,subject,"/admin/write-block"))
        if outcome["status"] != 401:
            raise ValueError("expected_actual_admin_authentication_rejection")
        self.event("access_denied", "실제 채팅 관리 API의 무인증 조회 거부. 침해 확정이 아닌 통제된 접근 시험", http_status=401)
        self.guard()
        pod=self.get("pod",subject["name"])
        if pod["metadata"]["uid"] != subject["uid"]:raise ValueError("subject_replaced")
        for policy in self.get("networkpolicies")["items"]:
            if selected(policy["spec"]["podSelector"],pod["metadata"]["labels"]):raise ValueError("concurrent_policy_change")
        policy={"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy",
            "metadata":{"name":self.plan["policy_name"],"namespace":self.ns,"labels":{"sova.dev/plan":approved_digest[:32]}},
            "spec":{"podSelector":{"matchLabels":{LABEL:subject["uid"]}},"policyTypes":["Ingress","Egress"],"ingress":[],"egress":[]}}
        made=self.effect("policy_create",lambda:self.kube("create","-f","-","-o","json",data=policy))
        save(self.root/'policy.json',{"name":made['metadata']['name'],"uid":made['metadata']['uid']})
        patch=[{"op":"test","path":"/metadata/uid","value":subject["uid"]},{"op":"test","path":"/metadata/resourceVersion","value":pod["metadata"]["resourceVersion"]},{"op":"add","path":"/metadata/labels/sova.dev~1response-run","value":subject["uid"]}]
        self.effect("pod_label",lambda:self.kube("-n",self.ns,"patch","pod",subject["name"],"--type=json","-p",json.dumps(patch),"-o","json"))
        self.event("isolation_requested", "선택한 실제 채팅 Pod 한 개에 ingress·egress 차단 적용. 효과 검증 중")
        verified=False
        for _ in range(5):
            self.guard()
            ingress=self.probe(peer,subject);egress=self.probe(subject,peer);control=self.probe(peer,peer)
            if not ingress['connected'] and not egress['connected'] and control['status']==200:
                verified=True;break
            time.sleep(2)
        if not verified:raise ValueError("containment_probe_failed_keep_hold")
        self.event("isolated", "선택 Pod의 새 수신·발신 연결 차단, 정상 이웃 응답 확인", ingress=ingress,egress=egress,positive_control=control)
        self.guard()
        # Termination fences the actual selected process; NetworkPolicy alone
        # does not establish that existing sockets have been terminated.
        delete={"apiVersion":"v1","kind":"DeleteOptions","preconditions":{"uid":subject['uid']},"gracePeriodSeconds":5}
        self.effect("pod_fence",lambda:self.kube("delete","--raw",f"/api/v1/namespaces/{self.ns}/pods/{subject['name']}","-f","-",data=delete))
        self.event("fencing", "선택 UID의 프로세스 종료와 기존 컨트롤러의 대체 기동 확인 중")
        original={p['uid'] for p in self.plan['original_pods']}
        replacement=None
        for _ in range(60):
            dep=self.guard();pods=self.get('pods')['items']
            selected_pods=[p for p in pods if selected(dep['spec']['selector'],p['metadata'].get('labels',{}))]
            fresh=[p for p in selected_pods if p['metadata']['uid'] not in original and ready(p)]
            if not any(p['metadata']['uid']==subject['uid'] for p in pods) and len(selected_pods)==dep['spec']['replicas'] and all(ready(p) for p in selected_pods) and len(fresh)==1:
                replacement=fresh[0];break
            time.sleep(2)
        if replacement is None:raise ValueError('replacement_not_verified')
        ref={'name':replacement['metadata']['name'],'uid':replacement['metadata']['uid'],'ip':replacement['status']['podIP']}
        image=replacement['status']['containerStatuses'][0]['imageID']
        if image!=subject['image_id']:raise ValueError('replacement_artifact_changed')
        if self.probe(peer,ref)['status']!=200 or self.probe(peer,ref,'/readyz')['status']!=200:raise ValueError('replacement_readiness_failed')
        slices=self.get('endpointslices')['items']
        endpoints=[e for s in slices if s['metadata'].get('labels',{}).get('kubernetes.io/service-name')=='demo-chat' for e in s.get('endpoints',[])]
        if not any(e.get('targetRef',{}).get('uid')==ref['uid'] and e.get('conditions',{}).get('ready') is True for e in endpoints):raise ValueError('replacement_not_serving')
        self.event("replaced", "원본 UID 소멸·동일 이미지 대체 Pod Ready·Service 연결 확인", replacement=ref,image_id=image,original_absent=True,ready_replicas=len(selected_pods))
        # Only the owned policy, and only after its selected Pod has gone.
        policy_id=json.loads((self.root/'policy.json').read_text())
        self.effect('policy_cleanup',lambda:self.kube('delete','--raw',f"/apis/networking.k8s.io/v1/namespaces/{self.ns}/networkpolicies/{policy_id['name']}",'-f','-',data={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':policy_id['uid']}}))
        self.event("complete", "실제 채팅 Pod의 부분 격리·교체 검증 완료. 보안 패치·침해 해소·AWS 이전 완료 판정은 별도")


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--state',required=True);p.add_argument('--cluster-uid');p.add_argument('--approved-digest');a=p.parse_args()
    runner=Runner(a.state)
    try:
        if a.action=='prepare':runner.prepare(a.cluster_uid)
        else:runner.run(a.approved_digest)
    except Exception:
        if hasattr(runner,'plan'):
            runner.event('attention','결과 확인 필요. 자동 재실행·격리 해제하지 않음')
        raise

if __name__=='__main__':main()
