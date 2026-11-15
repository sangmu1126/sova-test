"""Deployment configuration of the service processes: secrets come from
mounted files, a worker's owner ID names its pod and process instance, and
the execution file is checked before any connection is opened."""

import json
from uuid import uuid4

import pytest

from sova.domain.contracts import ContractError
from sova.service.catalog import catalog_from
from sova.service.config import ServiceConfig, WorkerConfig
from sova.service.quiescence import KubernetesWorkerQuiescence, pod_owner


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def service_env(tmp_path, **extra):
    return {"SOVA_DATABASE_URL_FILE": write(tmp_path, "db", "host=db user=sova_runtime\n"),
            "SOVA_OIDC_ISSUER": "https://idp.example/realms/sova",
            "SOVA_OIDC_AUDIENCE": "sova-api",
            "SOVA_OIDC_JWKS_URL": "https://idp.example/realms/sova/protocol/openid-connect/certs",
            "SOVA_REGISTRY_VERIFIER_VERSION": "registry-attestor-1",
            "SOVA_EVIDENCE_VERIFIER": "handoff-verifier-1",
            "SOVA_EVIDENCE_ISSUER": "handoff-producer",
            "SOVA_WITNESS": f"file:{tmp_path}", **extra}


def worker_env(tmp_path, **extra):
    return {**service_env(tmp_path), "SOVA_TENANTS": "tenant-a, tenant-b",
            "SOVA_EVIDENCE_KEY_FILE": write(tmp_path, "key", "11" * 32),
            "SOVA_EVIDENCE_KEY_ID": "k1",
            "SOVA_EXECUTION_FILE": write(tmp_path, "exec", json.dumps({"version": 1})),
            "POD_NAME": "sova-worker-7c9f-x2k4p", "POD_UID": str(uuid4()),
            "POD_NAMESPACE": "sova-system", **extra}


def test_secrets_are_read_from_mounted_files_never_from_the_environment(tmp_path):
    config = ServiceConfig.from_env(service_env(tmp_path))
    assert config.database_url == "host=db user=sova_runtime"
    with pytest.raises(ContractError):
        ServiceConfig.from_env(service_env(tmp_path, SOVA_DATABASE_URL_FILE=""))
    with pytest.raises(ContractError):
        ServiceConfig.from_env(service_env(
            tmp_path, SOVA_DATABASE_URL_FILE=write(tmp_path, "empty", "  \n")))


def test_the_jwks_is_fetched_over_https_except_for_a_loopback_test_issuer(tmp_path):
    with pytest.raises(ContractError):
        ServiceConfig.from_env(service_env(
            tmp_path, SOVA_OIDC_JWKS_URL="http://idp.example/certs"))
    ServiceConfig.from_env(service_env(
        tmp_path, SOVA_OIDC_ISSUER="http://127.0.0.1:58080/realms/sova",
        SOVA_OIDC_JWKS_URL="http://keycloak.test-env.svc:8080/realms/sova/certs"))


def test_a_workers_owner_id_names_its_pod_and_is_new_at_every_start(tmp_path):
    env = worker_env(tmp_path)
    first, second = WorkerConfig.from_env(env), WorkerConfig.from_env(env)
    assert first.tenants == ("tenant-a", "tenant-b")
    assert first.owner_id != second.owner_id  # a restarted container is another owner
    assert pod_owner(first.owner_id) == (env["POD_NAME"], env["POD_UID"], first.instance)
    with pytest.raises(ContractError):
        WorkerConfig.from_env(worker_env(tmp_path, SOVA_LEASE_SECONDS="600",
                                         SOVA_WORKER_STALL_SECONDS="300"))
    with pytest.raises(ContractError):
        WorkerConfig.from_env(worker_env(
            tmp_path, SOVA_EVIDENCE_KEY_FILE=write(tmp_path, "short", "11" * 16)))


def test_owner_ids_that_name_no_pod_are_never_proved_gone():
    uid = str(uuid4())
    assert pod_owner(f"worker-a.{uid}") == ("worker-a", uid, None)
    assert pod_owner("worker-a") is None
    assert pod_owner(f"worker-a.{uid}.NOT-HEX") is None
    assert pod_owner("worker-a.uid-of-a") is None
    with pytest.raises(ContractError):  # a verifier must know its own instance
        KubernetesWorkerQuiescence(pod_running=lambda n, u: None, clock=lambda: None,
                                   this_owner=f"worker-a.{uid}")


def application(tmp_path, **changes):
    dsn = write(tmp_path, "dsn", "host=orders-db user=postgres")
    app = {"tenant_id": "tenant-a", "application_id": "orders",
           "adapter_id": "postgres-offline-copy", "mode": "offline_copy",
           "databases": {db: {"dsn_file": dsn} for db in ("src", "tgt", "scratch", "other")},
           "source_db": "src", "target_db": "tgt", "scratch_db": "scratch",
           "other_db": "other", "app_role": "orders_app",
           "writer_roles": ["orders_app", "orders_batch"], "tables": [["orders", "id"]],
           "schema_sql_file": write(tmp_path, "schema.sql", "CREATE TABLE orders (id int)"),
           "probe_insert": "INSERT INTO orders VALUES (-1)",
           "route": {"kind": "kubernetes_service", "registration_file": "unused",
                     "service": "orders", "sides": {"src": {"side": "a"},
                                                    "tgt": {"side": "b"}}}}
    app.update(changes)
    return app


class FakeRoute:
    def get(self):
        return "src"

    def set(self, ref):
        pass


def test_the_execution_file_builds_one_factory_per_adapter_resolving_each_application(
        tmp_path):
    catalog, close = catalog_from(
        {"version": 1, "applications": [application(tmp_path)]},
        route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "postgres-offline-copy", None)
    assert execution.adapter.adapter_id == "postgres-offline-copy"
    with pytest.raises(ContractError):  # configured for one application only
        catalog.build("tenant-b", "orders", "postgres-offline-copy", None)
    with pytest.raises(ContractError):
        catalog.build("tenant-a", "orders", "another-adapter", None)
    close()


@pytest.mark.parametrize("changes", [
    {"source_db": "elsewhere"},
    {"app_role": "not_a_writer"},
    {"tables": [["orders"]]},
    {"mode": "dump_and_hope"},
    {"route": {"kind": "in_memory"}},
    {"unexpected": True},
])
def test_an_execution_file_that_does_not_add_up_is_refused(tmp_path, changes):
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [application(tmp_path, **changes)]},
                     route_for=lambda spec: FakeRoute())


def test_one_application_cannot_be_configured_twice_or_one_adapter_in_two_modes(tmp_path):
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [application(tmp_path)] * 2},
                     route_for=lambda spec: FakeRoute())
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [
            application(tmp_path),
            application(tmp_path, application_id="billing", mode="logical_replication")]},
            route_for=lambda spec: FakeRoute())
