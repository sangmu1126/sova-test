"""Independent public-code exercise verifier, not a production security verdict.

No candidate code executes until its bytes match the separately reviewed
reference. The agent can write a candidate, never this verifier or reference.
Promotion still requires Sova's own policy, approval and image checks.
"""

import asyncio
import hashlib
import os
from pathlib import Path
import stat
import types


def read_regular(path, *, limit=16384):
    path = Path(path)
    if path.parent.is_symlink():
        raise ValueError("Candidate parent must not be a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError("Bounded regular candidate file required")
        raw = f.read(limit + 1)
        after = os.fstat(f.fileno())
        if len(raw) > limit or (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            raise ValueError("Candidate changed while reading")
        return raw


async def boundary_checks(cls):
    checks = {}

    async def run(name, events, expected, *, headers=(), delay=0, scope_type="http"):
        delivered, sent = [], []
        async def app(scope, receive, send):
            delivered.append(scope["type"])
            if scope["type"] == "http":
                delivered.append((await receive())["body"])
                await send({"type":"http.response.start","status":200,"headers":[]})
                await send({"type":"http.response.body","body":b"ok"})
        async def receive():
            if delay:
                await asyncio.sleep(delay)
            return events.pop(0) if events else {"type":"http.disconnect"}
        async def send(event):
            sent.append(event)
        await cls(app,limit=4,seconds=.01)({"type":scope_type,"headers":list(headers)},receive,send)
        statuses=[e["status"] for e in sent if e["type"]=="http.response.start"]
        if expected is None:
            ok=not statuses and not delivered
        elif expected == "websocket":
            ok=delivered==["websocket"] and not statuses
        else:
            ok=statuses==[expected] and (expected==200 or not delivered)
        checks[name]=ok

    def chunks(*values):
        return [{"type":"http.request","body":v,"more_body":i<len(values)-1} for i,v in enumerate(values)]

    await run("exact_limit",chunks(b"ab",b"cd"),200)
    await run("aggregate_limit",chunks(b"abc",b"de"),413)
    await run("single_chunk_limit",chunks(b"abcde"),413)
    await run("declared_limit",chunks(b""),413,headers=((b"content-length",b"5"),))
    await run("duplicate_length",chunks(b"a"),413,headers=((b"content-length",b"1"),(b"Content-Length",b"1")))
    await run("invalid_length",chunks(b"a"),413,headers=((b"content-length",b"-1"),))
    await run("length_mismatch",chunks(b"abc"),400,headers=((b"content-length",b"4"),))
    await run("deadline",chunks(b"a"),408,delay=.03)
    await run("disconnect",[{"type":"http.disconnect"}],None)
    await run("websocket_passthrough",[],"websocket",scope_type="websocket")
    return checks


def verify(candidate, reference):
    reference_raw=read_regular(reference)
    raw=read_regular(candidate)
    if hashlib.sha256(raw).digest()!=hashlib.sha256(reference_raw).digest():
        return {"passed":False,"reason":"candidate_outside_reviewed_patch","candidate_executed":False}
    module=types.ModuleType("reviewed_boundary_candidate")
    exec(compile(raw,"reviewed_boundary_candidate","exec"),module.__dict__)
    checks=asyncio.run(boundary_checks(module.RequestBodyBoundary))
    return {"passed":all(checks.values()),"checks":checks,"candidate_sha256":hashlib.sha256(raw).hexdigest(),
        "candidate_executed":True,"operational_qualification":False}


if __name__ == "__main__":
    import json
    result=verify("/workspace/http_boundary.py","/grader/reference.py")
    Path("/logs/verifier").mkdir(parents=True,exist_ok=True)
    Path("/logs/verifier/result.json").write_text(json.dumps(result,indent=2)+"\n")
    Path("/logs/verifier/reward.txt").write_text("1.0\n" if result["passed"] else "0.0\n")
    print(json.dumps(result))
