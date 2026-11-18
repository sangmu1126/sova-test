"""Opt-in, fixed-scope native GET bridge for the local operator console.

The upstream credential is owner-private and stays in this process. This is a
single-operator local tool, not delegated enterprise authentication. It cannot
admit, approve, abort, reconcile or execute a command.
"""

from datetime import UTC, datetime
import http.client
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from sova.adapters.kubernetes.https import private_text
from sova.adapters.registration.graph_codec import json_document
from sova.domain.contracts import ContractError, digest, identifier
from sova.domain.handoff_authority import HandoffState
from sova.service.recovery_status import VERSION, PENDING, MAX_EFFECTS, next_steps


REASONS = {"EFFECT_RESULT_UNKNOWN", "RECOVERY_PENDING", "OWNER_EXPIRED_UNSETTLED",
           "TERMINAL_WITH_PENDING_EFFECTS", "APPLICATION_PENDING_EFFECTS", "SECURITY_HOLD",
           "AUTHORITY_HOLD", "OBSERVATION_UNAVAILABLE"}


def project_status(value, scope, *, now=None):
    """Reject stale/mismatched data; copy only whitelisted, non-secret fields."""
    now = now or datetime.now(UTC)
    try:
        if value.get("state") is None:
            raise ValueError
        if value["observation_version"] != VERSION:
            raise ValueError
        actual = value["scope"]
        if any(actual.get(k) != v for k, v in scope.items()):
            raise ValueError
        digest(actual["plan_digest"])
        if actual["operation_id"] is not None:
            identifier(actual["operation_id"])
        observed = datetime.fromisoformat(value["observed_at"])
        expires = datetime.fromisoformat(value["expires_at"])
        if (observed.tzinfo is None or expires.tzinfo is None
                or not 0 < (expires-observed).total_seconds() <= 5
                or (observed-now).total_seconds() > 1 or now >= expires):
            raise ValueError
        state = str(HandoffState(value["state"]))
        identifier(value["owner_environment"])
        if (type(value["epoch"]) is not int or value["epoch"] < 1
                or type(value["next_operation"]) is not int or value["next_operation"] < 0
                or type(value["activation_possible"]) is not bool):
            raise ValueError
        r = value["recovery"]
        security = r["security_holds"]
        ids = security["hold_ids"]
        if (type(ids) is not list or len(ids) > 256
                or type(security["active_count"]) is not int
                or security["active_count"] != len(ids)
                or type(security["operation_hold"]) is not bool):
            raise ValueError
        ids = [identifier(hold_id) for hold_id in ids]
        if len(set(ids)) != len(ids):
            raise ValueError
        security = {"active_count": len(ids), "hold_ids": ids,
                    "operation_hold": security["operation_hold"]}
        security_blocked = bool(ids) or security["operation_hold"]
        if r["automatic_replay_allowed"] is not False or r["authorizes_execution"] is not False:
            raise ValueError
        valid = ((r["status"] == "required" and r["required"] is True
                  and r["reason"] in REASONS - {"OBSERVATION_UNAVAILABLE"})
                 or (r["status"] == "clear" and r["required"] is False and r["reason"] is None)
                 or (r["status"] == "unavailable" and r["required"] is None
                     and r["reason"] == "OBSERVATION_UNAVAILABLE"))
        if (not valid or (security_blocked and r["status"] == "clear")
                or (r["reason"] == "SECURITY_HOLD" and not security_blocked)
                or r["next_steps"] != next_steps(r["reason"], security)):
            raise ValueError
        effects = r["pending_effects"]
        if not isinstance(effects, list) or len(effects) > MAX_EFFECTS:
            raise ValueError
        pending = []
        seen = set()
        for e in effects:
            fingerprint = digest(e["fingerprint"])
            if (fingerprint in seen or e["status"] not in PENDING
                    or type(e["revision"]) is not int or e["revision"] < 1):
                raise ValueError
            seen.add(fingerprint)
            pending.append({"fingerprint": fingerprint, "action": identifier(e["action"]),
                            "status": e["status"], "revision": e["revision"]})
        if any(e["status"] == "unknown" for e in pending) and r["status"] != "required":
            raise ValueError
        return {"observation_version": value["observation_version"],
                "scope": {**scope, "plan_digest": actual["plan_digest"],
                          "operation_id": actual["operation_id"]},
                "state": state, "epoch": value["epoch"],
                "next_operation": value["next_operation"],
                "owner_environment": value["owner_environment"],
                "activation_possible": value["activation_possible"],
                "observed_at": value["observed_at"], "expires_at": value["expires_at"],
                "recovery": {"status": r["status"], "required": r["required"],
                    "reason": r["reason"], "pending_effects": pending,
                    "security_holds": security,
                    "next_steps": list(r["next_steps"]),
                    "automatic_replay_allowed": False, "authorizes_execution": False}}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ContractError("Native status observation unavailable") from exc


class NativeStatusBridge:
    def __init__(self, config_file):
        config_file = Path(config_file).absolute()
        config = json_document(private_text(config_file, limit=16384), limit=16384)
        if type(config) is not dict or set(config) != {
            "origin", "tenant_id", "application_id", "unit_digest", "token_file"
        }:
            raise ContractError("Fixed native status registration required")
        endpoint = urlsplit(config["origin"])
        if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1"
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.path or endpoint.query or endpoint.fragment
                or not endpoint.port or config["origin"] != f"http://127.0.0.1:{endpoint.port}"):
            raise ContractError("Explicit loopback-only native API required")
        self.port = endpoint.port
        self.scope = {"tenant_id": identifier(config["tenant_id"]),
                      "application_id": identifier(config["application_id"]),
                      "unit_digest": digest(config["unit_digest"])}
        token_file = Path(config["token_file"])
        if not token_file.is_absolute():
            raise ContractError("Absolute private credential path required")
        self.token_file = token_file
        self.path = (f'/v1/tenants/{quote(self.scope["tenant_id"], safe="")}/applications/'
                     f'{quote(self.scope["application_id"], safe="")}/units/{self.scope["unit_digest"]}')

    def state(self):
        token = private_text(self.token_file, limit=16384).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ContractError("Native status credential unavailable")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request("GET", self.path, headers={"Authorization": "Bearer " + token,
                         "Accept": "application/json"})
            response = conn.getresponse()
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "application/json":
                raise ContractError("Native status observation unavailable")
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ContractError("Native status observation unavailable")
            return project_status(json_document(raw.decode("utf-8"), limit=512 * 1024), self.scope)
        except (OSError, http.client.HTTPException, UnicodeError) as exc:
            raise ContractError("Native status observation unavailable") from exc
        finally:
            conn.close()
