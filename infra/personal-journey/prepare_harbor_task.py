"""Stage a public-code Harbor exercise, with distinct build contexts.

No model call, image build, deployment credential, business data or admission.
The exercise reintroduces one known bug in a COPY of Sova's public boundary.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    os.umask(0o077)
    a.output.mkdir(mode=0o700,exist_ok=False)
    repo=Path(__file__).resolve().parents[2]
    reference=(repo/"src/sova/service/http_boundary.py").read_bytes()
    needle=b"if len(data) + len(chunk) > self.limit:"
    if reference.count(needle)!=1:
        raise ValueError("Public reviewed reference changed; re-review the exercise")
    buggy=reference.replace(needle,b"if len(chunk) > self.limit:")
    task=a.output/"task"
    for name in ("environment","tests","solution"):
        (task/name).mkdir(parents=True)
    (task/"instruction.md").write_text("Repair /workspace/http_boundary.py so the request body limit applies to the total bytes across all HTTP chunks. Change only that condition. Preserve existing malformed-length, disconnect, deadline and WebSocket behavior. Do not install packages, use the network, or change tests. This is a public code exercise and has no production access.\n")
    (task/"solution/solve.sh").write_text("#!/bin/sh\nset -eu\npython - <<'PY'\nfrom pathlib import Path\np=Path('/workspace/http_boundary.py')\ns=p.read_text();old='if len(chunk) > self.limit:';new='if len(data) + len(chunk) > self.limit:'\nassert s.count(old)==1\np.write_text(s.replace(old,new))\nPY\n")
    (task/"tests/test.sh").write_text("#!/bin/sh\nset -eu\nexec python /grader/verify.py\n")
    (task/"environment/docker-compose.yaml").write_text("services:\n  main:\n    network_mode: none\n    user: '10001:10001'\n    cap_drop: [ALL]\n    security_opt: [no-new-privileges:true]\n    pids_limit: 64\n    cpus: 0.25\n    mem_limit: 256m\n    tmpfs: ['/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777']\n")
    base=(repo/"examples/order-canary/Dockerfile").read_text().split("COPY requirements.lock",1)[0]
    base += "COPY requirements.lock /requirements.lock\nRUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /requirements.lock && python -m pip uninstall --yes pip && apk add --no-cache 'bash=5.3.9-r1' 'tmux=3.7c-r0' && addgroup -g 10001 exercise && adduser -D -H -u 10001 -G exercise exercise && mkdir -p /workspace /grader /logs/agent /logs/verifier /logs/artifacts && chown -R 10001:10001 /workspace /logs\n"
    hashes={}
    for name in ("agent","verifier"):
        context=a.output/(name+"-context")
        context.mkdir()
        shutil.copyfile(repo/"examples/order-canary/requirements.lock",context/"requirements.lock")
        tail=""
        if name=="agent":
            (context/"http_boundary.py").write_bytes(buggy)
            tail="COPY --chown=10001:10001 http_boundary.py /workspace/http_boundary.py\n"
        else:
            (context/"reference.py").write_bytes(reference)
            shutil.copyfile(Path(__file__).with_name("harbor_boundary_verifier.py"),context/"verify.py")
            tail="COPY reference.py verify.py /grader/\nRUN chmod 0444 /grader/*.py\n"
        (context/"Dockerfile").write_text(base+tail+"USER 10001:10001\nWORKDIR /workspace\nCMD [\"sh\",\"-c\",\"sleep infinity\"]\n")
        hashes[name]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in context.iterdir()}
    result={"version":1,"framework":"harbor==0.26.0","classification":"public-source-only",
        "reference_sha256":hashlib.sha256(reference).hexdigest(),"buggy_sha256":hashlib.sha256(buggy).hexdigest(),
        "contexts":hashes,"distinct_agent_verifier_images_required":True,"network_mode":"no-network",
        "model_called":False,"agent_executed":False,"production_changed":False,
        "next":"Build/scan immutable images; bind their actual registry digests in task.toml; Oracle baseline precedes AI test"}
    (a.output/"preparation.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({"public_exercise_prepared":True,"contexts":2,"model_called":False}))


if __name__=="__main__":
    main()
