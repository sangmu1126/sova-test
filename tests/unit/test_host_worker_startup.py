"""Host execution cannot borrow Pod identity or auto-clear remote uncertainty."""

import os
from types import SimpleNamespace

import pytest
from test_service_deployment import worker_env

from sova.domain.contracts import ContractError
from sova.service import main
from sova.service.config import LedgerConfig, ServiceConfig, WorkerConfig


def host_env(tmp_path, **extra):
    env = worker_env(tmp_path, SOVA_WORKER_IDENTITY="host", **extra)
    for name in ("POD_NAME", "POD_UID", "POD_NAMESPACE"):
        env.pop(name)
    return env


def test_host_owner_is_actual_process_and_new_at_every_start(tmp_path):
    env = host_env(tmp_path)
    first, second = WorkerConfig.from_env(env), WorkerConfig.from_env(env)
    assert first.owner_id.startswith(f"host.{os.getpid()}.")
    assert first.owner_id != second.owner_id
    assert first.pod_namespace == ""


@pytest.mark.parametrize("name", ["POD_NAME", "POD_UID", "POD_NAMESPACE"])
def test_host_worker_refuses_borrowed_pod_identity(tmp_path, name):
    env = host_env(tmp_path)
    env[name] = "borrowed"
    with pytest.raises(ContractError, match="must not claim"):
        WorkerConfig.from_env(env)


def test_unknown_identity_mode_is_not_silently_treated_as_host(tmp_path):
    with pytest.raises(ContractError, match="SOVA_WORKER_IDENTITY"):
        WorkerConfig.from_env(worker_env(tmp_path, SOVA_WORKER_IDENTITY="local"))


def test_real_startup_path_uses_native_worker_without_pod_observation(tmp_path, monkeypatch):
    from sova.service import catalog, quiescence, worker

    seen = {}
    config = SimpleNamespace(evidence_issuer="personal-qualification")
    monkeypatch.setattr(LedgerConfig, "from_env", lambda env: config)
    monkeypatch.setattr(catalog, "catalog_from", lambda execution: ("native-catalog", lambda: seen.update(closed=True)))
    monkeypatch.setattr(main, "_connect", lambda config: "real-configured-connect")
    monkeypatch.setattr(main, "_settings", lambda config: "native-settings")
    monkeypatch.setattr(main.signal, "signal", lambda *args: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Host worker must not read or manufacture Pod state")

    monkeypatch.setattr(quiescence, "in_cluster_pod_state", forbidden)
    monkeypatch.setattr(quiescence, "KubernetesWorkerQuiescence", forbidden)

    class NativeWorker:
        def __init__(self, connect, **kwargs):
            seen.update(kwargs)
            self.stopping = SimpleNamespace(set=lambda: None)

        def run(self, tenants, **kwargs):
            seen["tenants"] = tenants

    def health(worker, stall_seconds, port, *, host):
        seen["health_bind"] = host
        return SimpleNamespace(shutdown=lambda: seen.update(health_stopped=True))

    monkeypatch.setattr(worker, "HandoffWorker", NativeWorker)
    monkeypatch.setattr(main, "_health_server", health)
    assert main.run_worker(host_env(tmp_path)) == 0
    assert seen["quiescence"] is None
    assert seen["catalog"] == "native-catalog"
    assert seen["owner_id"].startswith(f"host.{os.getpid()}.")
    assert seen["health_bind"] == "127.0.0.1"
    assert seen["closed"] and seen["health_stopped"]


def test_worker_does_not_need_a_fictional_identity_provider_but_api_still_does(tmp_path):
    env = host_env(tmp_path)
    for name in ("SOVA_OIDC_ISSUER", "SOVA_OIDC_AUDIENCE", "SOVA_OIDC_JWKS_URL"):
        env.pop(name)
    assert LedgerConfig.from_env(env).database_url
    with pytest.raises(ContractError, match="SOVA_OIDC_ISSUER"):
        ServiceConfig.from_env(env)
