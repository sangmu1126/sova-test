import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "qualification_imported_identity",
    Path(__file__).resolve().parents[1] / "qualification_imported_identity.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("security", [False, True])
def test_real_pkce_clients_have_separate_trust_authorities_and_no_password_grant(
    security,
):
    realm, users, document = MODULE.definition(
        SimpleNamespace(origin="http://127.0.0.1:50123", tenant="personal"),
        client_id="sova-workspace",
        security_roles=security,
    )
    assert realm == document["realm"]
    assert users["planner"]["subject"] != users["approver"]["subject"]
    accounts = {u["username"]: u for u in document["users"]}
    assert "sova:attestor" not in accounts["approver"]["realmRoles"]
    assert "sova:trust_approver" not in accounts["planner"]["realmRoles"]
    assert "sova:trust_requester" not in accounts["approver"]["realmRoles"]
    assert ("sova:trust_approver" in accounts["approver"]["realmRoles"]) is security
    client = document["clients"][0]
    assert client["publicClient"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["implicitFlowEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == ["http://127.0.0.1:50123/workspace/callback"]


def test_import_passwords_enter_only_private_stdin_and_generated_filename(monkeypatch):
    realm, users, document = MODULE.definition(
        SimpleNamespace(origin="http://127.0.0.1:50123", tenant="personal"),
        client_id="sova-workspace",
        security_roles=True,
    )
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        for user in users.values():
            assert user["password"] not in " ".join(argv)
            assert user["password"] in kwargs["input"]
        assert json.loads(kwargs["input"]) == document
        assert "env" not in kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    MODULE.transfer("owned-container-id", realm, document)
    argv, kwargs = observed[0]
    assert argv[:5] == ["docker", "exec", "-i", "owned-container-id", "/bin/bash"]
    assert argv[-1] == "/opt/keycloak/data/import/" + realm + "-realm.json"
    assert "chmod 400" in argv[-3] and "umask 077" in argv[-3]
    assert kwargs["timeout"] == 30


@pytest.mark.parametrize(
    "realm", ["../other", "journey-" + ";" * 32, "journey-" + "a" * 31]
)
def test_unowned_or_shell_like_import_paths_never_reach_docker(monkeypatch, realm):
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("Unowned filename must not execute"),
    )
    with pytest.raises(ValueError, match="Owned generated realm"):
        MODULE.transfer("owned-container-id", realm, {"realm": realm})
