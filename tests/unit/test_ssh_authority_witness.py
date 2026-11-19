"""Boundary tests; these do not claim a live SSH or storage qualification."""

import hashlib
import json
import subprocess
import sys

import pytest

from sova.adapters.file_authority_witness import FileAuthorityWitness
import sova.adapters.ssh_authority_witness as transport
from sova.adapters.ssh_authority_witness import SSHAuthorityWitness, handle
from sova.domain.contracts import ContractError, canonical_json
from sova.service.config import LedgerConfig


def request(**changes):
    return {"tenant_id": "tenant", "operation": "latest", "application_id": "app",
            "unit_digest": "a" * 64, **changes}


def endpoint(tmp_path, **changes):
    key, known = tmp_path / "key", tmp_path / "known"
    if not key.exists():
        key.write_text("test-key")
        key.chmod(0o600)
        known.write_text("test-public-host-key")
    return {"host": "127.0.0.1", "port": 23456, "user": "sova-witness",
            "key_file": str(key), "known_hosts": str(known), **changes}


def test_existing_file_chain_preserved_without_remote_path_selection(tmp_path):
    witness = FileAuthorityWitness(tmp_path / "witness.log", tenant_id="tenant")
    assert handle(request(), witness)["value"] is None
    doc = request(operation="append", action="authority.claimed", record={"epoch": 1})
    assert handle(doc, witness)["value"] is None
    assert handle(request(), witness)["value"] == {"epoch": 1}
    assert witness.latest("app", "a" * 64) == {"epoch": 1}


@pytest.mark.parametrize("changes", [{"tenant_id": "other"}, {"path": "/tmp/other"},
                                    {"operation": "truncate"}, {"unit_digest": "invalid"}])
def test_invalid_cross_tenant_or_destructive_protocol_never_changes_witness(tmp_path, changes):
    path = tmp_path / "witness.log"
    with pytest.raises(ContractError):
        handle(request(**changes), FileAuthorityWitness(path, tenant_id="tenant"))
    assert not path.exists()


def test_client_pins_ssh_and_binds_response_to_request(tmp_path, monkeypatch):
    seen = []

    def run(command, raw, timeout):
        seen.append(command)
        return 0, json.dumps({
            "request_digest": hashlib.sha256(raw).hexdigest(), "value": {"epoch": 4}}).encode()

    monkeypatch.setattr(transport, "_exchange", run)
    client = SSHAuthorityWitness(tenant_id="tenant", **endpoint(tmp_path))
    assert client.latest("app", "a" * 64) == {"epoch": 4}
    assert "StrictHostKeyChecking=yes" in seen[0]
    assert "IdentitiesOnly=yes" in seen[0]
    assert "ClearAllForwardings=yes" in seen[0]
    assert "ControlPath=none" in seen[0]


def test_lost_append_response_is_not_automatically_retried(tmp_path, monkeypatch):
    calls = []

    def lost(command, raw, timeout):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(transport, "_exchange", lost)
    client = SSHAuthorityWitness(tenant_id="tenant", **endpoint(tmp_path))
    with pytest.raises(ContractError, match="no automatic retry"):
        client.append("app", "a" * 64, "claim", {"epoch": 2})
    assert len(calls) == 1


@pytest.mark.parametrize("response", [
    {"request_digest": "0" * 64, "value": None},
    {"request_digest": "0" * 64, "value": []},
    {"request_digest": "0" * 64, "value": None, "extra": True},
])
def test_mismatched_or_invalid_response_is_rejected(tmp_path, monkeypatch, response):
    monkeypatch.setattr(transport, "_exchange", lambda *a, **kw: (0, json.dumps(response).encode()))
    client = SSHAuthorityWitness(tenant_id="tenant", **endpoint(tmp_path))
    with pytest.raises(ContractError):
        client.latest("app", "a" * 64)


@pytest.mark.parametrize("change", [{"host": "-oProxyCommand=evil"}, {"user": "bad;id"},
                                  {"port": True}, {"port": 0}])
def test_configured_endpoint_cannot_supply_options_or_commands(tmp_path, change):
    with pytest.raises(ContractError):
        SSHAuthorityWitness(tenant_id="tenant", **endpoint(tmp_path, **change))


def test_private_key_permissions_are_required(tmp_path):
    settings = endpoint(tmp_path)
    (tmp_path / "key").chmod(0o644)
    with pytest.raises(ContractError):
        SSHAuthorityWitness(tenant_id="tenant", **settings)


def test_ledger_never_reuses_an_endpoint_for_an_unknown_tenant(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(canonical_json({"tenants": {"tenant": endpoint(tmp_path)}}))
    config = LedgerConfig("unused", "v1", "v1", "issuer", "ssh-file:" + str(path), {})
    select = config.witness_for()
    assert select("tenant").tenant_id == "tenant"
    with pytest.raises(ContractError):
        select("other")


def test_actual_child_response_is_bounded_before_consuming_unlimited_bytes():
    with pytest.raises(ContractError, match="exceeded bound"):
        transport._exchange([sys.executable, "-c",
            "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(b'x'*1000000)"], b"{}", 3)


def test_actual_hanging_child_has_a_bounded_deadline():
    with pytest.raises(subprocess.TimeoutExpired):
        transport._exchange([sys.executable, "-c",
            "import sys,time;sys.stdin.buffer.read();time.sleep(10)"], b"{}", 1)
