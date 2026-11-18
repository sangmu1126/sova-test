"""The deployment configuration must be executable by the real control ledger.

Invalid arguments are refused before catalog construction, connections, Pod
observation or health listeners. No DB or Kubernetes fixtures are contacted.
"""

import pytest
from test_service_deployment import worker_env

from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.domain.contracts import ContractError
from sova.service import main
from sova.service.config import WorkerConfig


@pytest.mark.parametrize("seconds", [10, 300])
def test_accepted_deployment_boundaries_are_accepted_by_the_control_ledger(tmp_path, seconds):
    config = WorkerConfig.from_env(worker_env(tmp_path, SOVA_LEASE_SECONDS=str(seconds)))
    assert config.lease_seconds == seconds
    PostgresExecutionOwnership.duration(config.lease_seconds)


def test_default_lease_and_stall_are_preserved(tmp_path):
    config = WorkerConfig.from_env(worker_env(tmp_path))
    assert (config.lease_seconds, config.stall_seconds) == (120, 600)
    PostgresExecutionOwnership.duration(config.lease_seconds)


@pytest.mark.parametrize("value", ["9", "301", "600", "900", "1_20", " 120 ", True])
def test_invalid_lease_never_becomes_a_worker_configuration(tmp_path, value):
    with pytest.raises(ContractError, match="SOVA_LEASE_SECONDS"):
        WorkerConfig.from_env(worker_env(tmp_path, SOVA_LEASE_SECONDS=value,
                                         SOVA_WORKER_STALL_SECONDS="1000"))


@pytest.mark.parametrize("value", ["120", "86401", "nan", True])
def test_stall_must_be_valid_seconds_and_exceed_the_lease(tmp_path, value):
    with pytest.raises(ContractError, match="SOVA_WORKER_STALL_SECONDS"):
        WorkerConfig.from_env(worker_env(tmp_path, SOVA_WORKER_STALL_SECONDS=value))


@pytest.fixture
def no_runtime_side_effects(monkeypatch):
    from sova.service import catalog, quiescence
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("runtime resource construction")
        raise AssertionError("Invalid startup input reached runtime resource construction")
    monkeypatch.setattr(catalog, "catalog_from", forbidden)
    monkeypatch.setattr(quiescence, "in_cluster_pod_state", forbidden)
    monkeypatch.setattr(main, "_connect", forbidden)
    monkeypatch.setattr(main, "_health_server", forbidden)
    return calls


@pytest.mark.parametrize("value", [0, 65536, True])
def test_invalid_health_port_is_refused_before_config_or_runtime(no_runtime_side_effects, value):
    with pytest.raises(ContractError, match="health_port"):
        main.run_worker({}, health_port=value)
    assert no_runtime_side_effects == []


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 600, 601, True])
def test_invalid_poll_interval_is_refused_before_any_runtime_resource(
        tmp_path, no_runtime_side_effects, value):
    with pytest.raises(ContractError, match="poll_seconds"):
        main.run_worker(worker_env(tmp_path), poll_seconds=value)
    assert no_runtime_side_effects == []


@pytest.mark.parametrize("value", ["301", "600"])
def test_unexecutable_lease_is_refused_before_catalog_and_connections(
        tmp_path, no_runtime_side_effects, value):
    with pytest.raises(ContractError, match="SOVA_LEASE_SECONDS"):
        main.run_worker(worker_env(tmp_path, SOVA_LEASE_SECONDS=value,
                                  SOVA_WORKER_STALL_SECONDS="1000"))
    assert no_runtime_side_effects == []
