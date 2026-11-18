"""Local read boundary: registration, secret custody and freshness, not cloud acceptance."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from native_status_bridge import NativeStatusBridge, project_status
from sova.domain.contracts import ContractError
from sova.service.recovery_status import NEXT_STEPS


SCOPE = {"tenant_id": "tenant-a", "application_id": "app-a", "unit_digest": "a"*64}


def observation():
    now = datetime.now(UTC)
    return {"observation_version": "handoff-recovery-status-v2", "state": "SOURCE_FENCED",
            "scope": {**SCOPE, "plan_digest": "b"*64, "operation_id": "operation-1"},
            "epoch": 2, "next_operation": 3, "owner_environment": "source", "activation_possible": False,
            "observed_at": now.isoformat(), "expires_at": (now+timedelta(seconds=5)).isoformat(),
            "recovery": {"status": "required", "required": True, "reason": "EFFECT_RESULT_UNKNOWN",
                "security_holds": {"active_count": 0, "hold_ids": [], "operation_hold": False},
                "pending_effects": [{"fingerprint": "c"*64, "action": "copy_content", "status": "unknown", "revision": 2}],
                "next_steps": list(NEXT_STEPS), "automatic_replay_allowed": False, "authorizes_execution": False}}


@pytest.mark.parametrize("mutation", [
    lambda v: v["scope"].update(tenant_id="another-tenant"),
    lambda v: v["scope"].update(application_id="another-app"),
    lambda v: v["scope"].update(unit_digest="0"*64),
    lambda v: v.update(expires_at=v["observed_at"]),
    lambda v: v.update(observed_at="2020-01-01T00:00:00Z", expires_at="2020-01-01T00:00:05Z"),
    lambda v: v.update(observed_at="2999-01-01T00:00:00Z", expires_at="2999-01-01T00:00:05Z"),
    lambda v: v.update(observed_at="2026-11-19T00:00:00"),
    lambda v: v.update(epoch=True),
    lambda v: v["recovery"].update(authorizes_execution=True),
    lambda v: v["recovery"].update(automatic_replay_allowed=True),
    lambda v: v["recovery"].update(status="clear", required=False, reason=None, next_steps=[]),
    lambda v: v["recovery"]["pending_effects"].append(deepcopy(v["recovery"]["pending_effects"][0])),
    lambda v: v["recovery"].update(next_steps=["RUN_AGAIN"]),
])
def test_untrusted_or_expired_projection_is_not_presented_as_clear(mutation):
    value = observation()
    mutation(value)
    with pytest.raises(ContractError):
        project_status(value, SCOPE)


def test_projection_does_not_forward_extra_secrets_or_command_parameters():
    value = observation()
    value.update(token="DO-NOT-EXPOSE", command_parameters={"password": "DO-NOT-EXPOSE"})
    value["recovery"]["pending_effects"][0]["token"] = "DO-NOT-EXPOSE"
    result = project_status(value, SCOPE)
    assert result["state"] == "SOURCE_FENCED" and result["recovery"]["required"] is True
    assert "DO-NOT-EXPOSE" not in json.dumps(result)


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(observation_version="handoff-recovery-status-v1"),
    lambda v: v["recovery"].pop("security_holds"),
    lambda v: v["recovery"]["security_holds"].update(active_count=True),
    lambda v: v["recovery"]["security_holds"].update(active_count=1),
    lambda v: v["recovery"]["security_holds"].update(active_count=2, hold_ids=["hold-a", "hold-a"]),
    lambda v: v["recovery"]["security_holds"].update(operation_hold="false"),
])
def test_governance_snapshot_cannot_be_omitted_or_miscounted(mutation):
    value = observation()
    mutation(value)
    with pytest.raises(ContractError):
        project_status(value, SCOPE)


def test_security_only_hold_uses_security_review_and_cannot_be_reported_clear():
    value = observation()
    value["recovery"].update(reason="SECURITY_HOLD", pending_effects=[],
        security_holds={"active_count": 1, "hold_ids": ["hold-a"], "operation_hold": False},
        next_steps=["REVIEW_SECURITY_HOLD"])
    assert project_status(value, SCOPE)["recovery"]["next_steps"] == ["REVIEW_SECURITY_HOLD"]
    value["recovery"].update(reason=None, status="clear", required=False)
    with pytest.raises(ContractError):
        project_status(value, SCOPE)


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


def config(tmp_path, **changes):
    token = private(tmp_path/"jwt", "test.payload.signature")
    return private(tmp_path/"config", json.dumps({**SCOPE, "origin": "http://127.0.0.1:9999",
                                                  "token_file": str(token), **changes}))


@pytest.mark.parametrize("origin", ["http://localhost:9999", "http://169.254.169.254:80",
    "https://example.com", "http://127.0.0.1:9999/", "http://user@127.0.0.1:9999",
    "http://127.0.0.1:9999?path=secret", "http://127.0.0.1:9999#fragment"])
def test_arbitrary_upstream_is_not_a_browser_proxy(tmp_path, origin):
    with pytest.raises(ContractError):
        NativeStatusBridge(config(tmp_path, origin=origin))


def test_private_registration_and_rotating_credential_are_enforced(tmp_path):
    path = config(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ContractError):
        NativeStatusBridge(path)
    path.chmod(0o600)
    bridge = NativeStatusBridge(path)
    token = tmp_path/"jwt"
    token.chmod(0o644)
    with pytest.raises(ContractError):
        bridge.state()
    token.unlink()
    token.symlink_to(path)
    with pytest.raises(ContractError):
        bridge.state()


def test_only_fixed_get_is_sent_and_denial_does_not_keep_a_cached_success(tmp_path):
    seen = []
    response_status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            data = json.dumps(observation()).encode()
            self.send_response(response_status[0])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        bridge = NativeStatusBridge(config(tmp_path, origin=f"http://127.0.0.1:{server.server_port}"))
        assert bridge.state()["recovery"]["required"] is True
        private(tmp_path/"jwt", "new.payload.signature")
        for denied in (401, 403, 404, 302, 503):
            response_status[0] = denied
            with pytest.raises(ContractError):
                bridge.state()
        assert len(seen) == 6
        assert {item[:2] for item in seen} == {("GET", "/v1/tenants/tenant-a/applications/app-a/units/"+"a"*64)}
        assert seen[0][2] == "Bearer test.payload.signature"
        assert all(item[2] == "Bearer new.payload.signature" for item in seen[1:])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
