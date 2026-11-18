"""Opt-in actual browser against an ephemeral console and the live native API.

Local operator identity is a fixture, not corporate delegated authentication.
No Kubernetes/remote control method is exposed or invoked by this helper.
"""
from datetime import UTC, datetime, timedelta
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace

from console_web import ConsoleWebBundle
from operator_auth import OperatorAuth
from security_console import handler


def observe_browser(h, bridge, expected, directory):
    tools = os.environ.get("SOVA_TEST_RECOVERY_BROWSER_TOOLS")
    if not tools:
        return {"executed": False, "reason": "Browser tools not explicitly supplied"}
    artifacts = os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS")
    if artifacts:
        directory = Path(artifacts)/("browser-"+h.case_name)
        directory.mkdir(mode=0o700)
    root = Path(__file__).resolve().parents[1]
    deadline = datetime.now(UTC)+timedelta(minutes=2)
    auth = OperatorAuth(deadline=deadline, clock=lambda: datetime.now(UTC))
    token, session = auth.exchange(auth.issue_bootstrap())
    console = SimpleNamespace(deadline=deadline, snapshot=lambda *args, **kwargs: {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    origin = f"http://127.0.0.1:{server.server_port}"
    server.RequestHandlerClass = handler(console, origin, auth,
        ui_bundle=ConsoleWebBundle(root/"console/dist"), native_bridge=bridge)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([os.environ["SOVA_TEST_RECOVERY_BROWSER_NODE"],
                str(root/"tests/qualification_recovery_browser.mjs")],
            input=json.dumps({"tools":tools, "executable":os.environ["SOVA_TEST_RECOVERY_BROWSER_EXECUTABLE"],
                "origin":origin, "cookie":token, "proof":session.csrf,
                "expires_at":session.expires_at.isoformat(), "directory":str(directory),
                "expected":expected}), capture_output=True, text=True, timeout=45)
        if result.returncode != 0:
            # Never persist command input, credentials or an unfiltered browser log.
            redacted = result.stderr.replace(token, "[redacted]").replace(session.csrf, "[redacted]")
            error = directory/"browser-error.log"
            error.write_text(redacted[-8000:])
            error.chmod(0o600)
            raise AssertionError("Run-owned browser assertion failed (credential input withheld)")
        evidence = json.loads(result.stdout)
        assert evidence["executed"] is True and evidence["mutation_requests"] == 0
        assert evidence["native_gets"] > 0 and evidence["page_errors"] == 0
        for name in ("desktop", "mobile"):
            path = directory/f"{name}.png"
            evidence[name+"_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return evidence
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("Run-owned console did not stop")
