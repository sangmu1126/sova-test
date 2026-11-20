"""Fail closed before changing unrelated TLS inputs or a different bridge."""

import copy
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


INFRA = Path(__file__).resolve().parents[2] / "infra/personal-journey"


def load(name, monkeypatch):
    monkeypatch.syspath_prepend(str(INFRA))
    spec = importlib.util.spec_from_file_location(
        "renewal_test_" + name, INFRA / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepared(tmp_path):
    files = tmp_path / "prepared"
    files.mkdir()
    (files / "entry.json").write_text(
        json.dumps(
            {
                "run_id": "b0be270e62",
                "namespace": "sj-b0be270e62",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                "certificate_sha256": "a" * 64,
            }
        )
    )
    secret = {
        "kind": "Secret",
        "metadata": {
            "name": "orders-entry-tls",
            "namespace": "sj-b0be270e62",
            "labels": {"sova.run": "b0be270e62"},
        },
        "immutable": True,
        "data": {"entry.pem": "opaque", "ca.pem": "opaque"},
    }
    (files / "resources.json").write_text(json.dumps({"items": [secret]}))
    return SimpleNamespace(
        prepared=files,
        artifacts=tmp_path / "result",
        previous_generation=3,
        generation=4,
    )


@pytest.mark.parametrize("old,new", [(3, 3), (3, 5), (0, 1), (32, 33)])
def test_tls_renewal_refuses_nonconsecutive_generations_without_calls(
    tmp_path, monkeypatch, old, new
):
    module = load("renew_entry", monkeypatch)
    args = prepared(tmp_path)
    args.previous_generation, args.generation = old, new
    monkeypatch.setattr(module, "get", lambda *_: pytest.fail("No live reads expected"))
    monkeypatch.setattr(module, "mutate", lambda *_: pytest.fail("No mutation allowed"))
    with pytest.raises(ValueError, match="consecutive"):
        module.renew_tls_only(args, {}, {})


@pytest.mark.parametrize("change", ["owner", "extra_secret", "mutable", "extra_data"])
def test_tls_renewal_refuses_changed_prepared_secret_before_live_mutation(
    tmp_path, monkeypatch, change
):
    module = load("renew_entry", monkeypatch)
    args = prepared(tmp_path)
    path = args.prepared / "resources.json"
    data = json.loads(path.read_text())
    secret = data["items"][0]
    if change == "owner":
        secret["metadata"]["labels"]["sova.run"] = "other"
    elif change == "extra_secret":
        data["items"].append(copy.deepcopy(secret))
    elif change == "mutable":
        secret["immutable"] = False
    else:
        secret["data"]["unrelated-key"] = "opaque"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(module, "get", lambda *_: pytest.fail("No live reads expected"))
    monkeypatch.setattr(module, "mutate", lambda *_: pytest.fail("No mutation allowed"))
    with pytest.raises(ValueError):
        module.renew_tls_only(args, {}, {})


def test_both_existing_tls_bindings_checked_before_secret_creation(
    tmp_path, monkeypatch
):
    module = load("renew_entry", monkeypatch)
    args = prepared(tmp_path)
    seen = []

    def get(kind, name):
        seen.append(name)
        return {
            "metadata": {"labels": {"sova.run": "b0be270e62"}},
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [
                            {
                                "name": "tls",
                                "secret": {
                                    "secretName": "orders-entry-tls-v3"
                                    if name.endswith("source")
                                    else "unexpected-generation"
                                },
                            }
                        ]
                    }
                }
            },
        }

    monkeypatch.setattr(module, "get", get)
    monkeypatch.setattr(module, "mutate", lambda *_: pytest.fail("No mutation allowed"))
    with pytest.raises(ValueError, match="Previous owned"):
        module.renew_tls_only(args, {}, {})
    assert seen == ["orders-entry-source", "orders-entry-target"]


def test_existing_foreign_bridge_address_refuses_all_firewall_changes(monkeypatch):
    module = load("renew_source", monkeypatch)
    rendered = module.script("pinned-secret-version", datetime.now(UTC))
    # Shell syntax and the actual embedded program are checked without running SSH/SSM.
    subprocess.run(
        ["bash", "-n"], input=rendered, text=True, check=True, capture_output=True
    )
    program = rendered.split("<<'RESTORE_OWNED_BRIDGE'\n", 1)[1].split(
        "\nRESTORE_OWNED_BRIDGE", 1
    )[0]
    calls = []

    def read(args):
        calls.append(args)
        return json.dumps(
            [{"addr_info": [{"family": "inet", "local": "192.0.2.1", "prefixlen": 24}]}]
        ).encode()

    monkeypatch.setattr(subprocess, "check_output", read)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("Foreign bridge must not mutate"),
    )
    with pytest.raises(ValueError, match="Unexpected existing bridge"):
        exec(compile(program, "<actual-renewal-bridge-program>", "exec"), {})
    assert calls == [["ip", "-j", "address", "show", "dev", "br-e727195d35bb"]]
