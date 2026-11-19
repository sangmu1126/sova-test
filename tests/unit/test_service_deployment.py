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


def mysql_application(tmp_path, **changes):
    settings = write(tmp_path, "mysql.json", json.dumps({"host": "orders-db", "port": 3306,
                                                          "user": "sova-admin"}))
    base = {"engine": "mysql", "adapter_id": "mysql-offline-copy",
            "databases": {db: {"dsn_file": settings} for db in ("src", "tgt", "scratch",
                                                               "other")},
            "app_role": "orders_app@%", "writer_roles": ["orders_app@%", "batch@%"],
            "zone_of": {"tgt": "zone-a"}}
    return application(tmp_path, **{**base, **changes})


def test_a_mysql_application_gets_the_mysql_adapter(tmp_path):
    catalog, close = catalog_from({"version": 1, "applications": [mysql_application(tmp_path)]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "mysql-offline-copy", None)
    assert type(execution.adapter).__name__ == "MySqlOfflineCopyAdapter"
    close()


@pytest.mark.parametrize("changes", [
    {"mode": "logical_replication"},  # no MySQL change-data-capture adapter
    {"engine": "oracle"},
    {"zone_of": {"tgt": 1}},
])
def test_a_mysql_entry_that_does_not_add_up_is_refused(tmp_path, changes):
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [mysql_application(tmp_path, **changes)]},
                     route_for=lambda spec: FakeRoute())


def test_mysql_connection_settings_are_checked(tmp_path):
    bad = write(tmp_path, "bad.json", json.dumps({"host": "db", "user": "u", "init_command":
                                                  "SET GLOBAL read_only=0"}))
    app = mysql_application(tmp_path, databases={
        db: {"dsn_file": bad} for db in ("src", "tgt", "scratch", "other")})
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [app]}, route_for=lambda spec: FakeRoute())


def volume_application(tmp_path, **changes):
    side = {"registration_file": "unused", "claims": ["board-data"],
            "app": ["deployment", "board"], "others": [["cronjob", "board-backup"]]}
    volume = {"source": side, "target": {**side, "claims": ["board-data-new"]},
              "routes": ["board_src", "board_tgt"],
              "agent_image": "docker.io/library/python@sha256:" + "5" * 64,
              "agent_key_file": write(tmp_path, "agent.key", "ab" * 32),
              "run_as": [10001, 10001], "write_probe": {"port": 8080, "path": "/probe"},
              "databases": ["board.db"], "zone_of": {"target": "zone-a"},
              "invariants": {"BOARD-01": {"database": "board.db",
                                          "violations": "SELECT id FROM posts WHERE 0"}},
              "scratch_claim": "sova-restore"}
    app = {"tenant_id": "tenant-a", "application_id": "board",
           "adapter_id": "kubernetes-volume-handoff", "engine": "kubernetes-volume",
           "mode": "offline_copy", "volume": volume,
           "route": {"kind": "kubernetes_service", "registration_file": "unused",
                     "service": "board", "sides": {"board_src": {"side": "a"},
                                                   "board_tgt": {"side": "b"}}}}
    for key, value in changes.items():
        (volume if key.startswith("volume.") else app)[key.removeprefix("volume.")] = value
    return app


def fake_volume_controls(spec):
    from types import SimpleNamespace

    from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls

    controls = object.__new__(KubernetesVolumeControls)  # nothing is contacted here
    controls.registration = SimpleNamespace(namespace="board")
    return controls


def test_a_volume_unit_gets_the_volume_adapter_and_its_probes(tmp_path):
    catalog, close = catalog_from({"version": 1, "applications": [volume_application(tmp_path)]},
                                  route_for=lambda spec: FakeRoute(),
                                  controls_for=fake_volume_controls)
    execution = catalog.build("tenant-a", "board", "kubernetes-volume-handoff", None)
    assert type(execution.adapter).__name__ == "KubernetesVolumeHandoffAdapter"
    assert execution.adapter.routes == ("board_src", "board_tgt")
    close()


@pytest.mark.parametrize("changes", [
    {"mode": "logical_replication"},  # a file system has no change log to follow
    {"volume.agent_image": "python:3.12-slim"},  # not pinned by digest
    {"volume.routes": ["board_src"]},
    {"volume.run_as": [0, 0]},  # never as root
    {"volume.write_probe": {"port": 8080}},
    {"volume.unexpected": True},
    {"databases": {}},  # a database entry's keys do not belong here
])
def test_a_volume_entry_that_does_not_add_up_is_refused(tmp_path, changes):
    with pytest.raises(ContractError):
        catalog = catalog_from({"version": 1, "applications": [
            volume_application(tmp_path, **changes)]}, route_for=lambda spec: FakeRoute(),
            controls_for=fake_volume_controls)[0]
        catalog.build("tenant-a", "board", "kubernetes-volume-handoff", None)


def session_entry(**changes):
    session = {"source": {"registration_file": "unused", "app": ["deployment", "game"]},
               "target": {"registration_file": "unused", "app": ["deployment", "game-new"]},
               "routes": ["game_src", "game_tgt", "game_paused"], "port": 8080,
               "image": "docker.io/library/python@sha256:" + "5" * 64, "run_as": [10001, 10001],
               "drain_seconds": 120, "on_timeout": "terminate"}
    session.update(changes)
    return {"adapter_id": "game-sessions", "engine": "kubernetes-session", "session": session,
            "route": {"kind": "kubernetes_service", "registration_file": "unused",
                      "service": "game", "sides": {}}}


class FakeServiceRoute(FakeRoute):
    def observe(self):
        return {"selector_side": "game_src", "ready_endpoints": 1, "foreign_endpoints": [],
                "route": "game_src"}

    def pause(self, ref):
        pass

    def paused(self, ref):
        return False, self.observe()

    def left(self, ref):
        return False


def test_a_whole_application_is_one_unit_of_a_volume_and_a_session_server(tmp_path):
    board = volume_application(tmp_path)
    entry = {"tenant_id": "tenant-a", "application_id": "board", "adapter_id": "board-whole",
             "engine": "composite", "mode": "offline_copy", "data": "board",
             "components": [{"name": "game", **session_entry()},
                            {"name": "board", "adapter_id": "board-volume",
                             "engine": "kubernetes-volume", "volume": board["volume"],
                             "route": board["route"]}]}
    catalog, close = catalog_from({"version": 1, "applications": [entry]},
                                  route_for=lambda spec: FakeServiceRoute(),
                                  controls_for=fake_volume_controls)
    execution = catalog.build("tenant-a", "board", "board-whole", None)
    assert type(execution.adapter).__name__ == "CompositeHandoffAdapter"
    assert [(n, type(a).__name__) for n, a in execution.adapter.components] == [
        ("game", "KubernetesSessionHandoffAdapter"), ("board", "KubernetesVolumeHandoffAdapter")]
    close()


@pytest.mark.parametrize("change", [
    lambda e: e.update(data="cache"),  # names no component
    lambda e: e["components"].pop(),  # one component is not a composite
    lambda e: e["components"][0].update(engine="postgresql"),
    lambda e: e["components"][0]["session"].update(routes=["game_src", "game_tgt"]),
    lambda e: e["components"][0]["session"].update(on_timeout="ignore"),
    lambda e: e.update(mode="logical_replication"),
])
def test_a_composite_entry_that_does_not_add_up_is_refused(tmp_path, change):
    board = volume_application(tmp_path)
    entry = {"tenant_id": "tenant-a", "application_id": "board", "adapter_id": "board-whole",
             "engine": "composite", "mode": "offline_copy", "data": "board",
             "components": [{"name": "game", **session_entry()},
                            {"name": "board", "adapter_id": "board-volume",
                             "engine": "kubernetes-volume", "volume": board["volume"],
                             "route": board["route"]}]}
    change(entry)
    with pytest.raises(ContractError):
        catalog = catalog_from({"version": 1, "applications": [entry]},
                               route_for=lambda spec: FakeServiceRoute(),
                               controls_for=fake_volume_controls)[0]
        catalog.build("tenant-a", "board", "board-whole", None)


def test_a_database_unit_may_switch_its_traffic_at_a_gateway(tmp_path):
    seen = []
    app = application(tmp_path, route={
        "kind": "gateway_httproute", "registration_file": "unused", "route": "orders",
        "parents": [["infra", "public"]], "sides": {"src": ["orders-a", 80], "tgt": ["orders-b", 80]},
        "probe": {"url": "https://orders.example.com/healthz", "header": "X-Sova-Side",
                  "values": {"src": "a", "tgt": "b"}}})
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: seen.append(spec["kind"]) or FakeRoute())
    catalog.build("tenant-a", "orders", "postgres-offline-copy", None)
    assert seen == ["gateway_httproute"]
    close()


def test_sessions_drain_only_behind_a_route_store_that_can_pause(tmp_path):
    entry = {"tenant_id": "tenant-a", "application_id": "game", **session_entry(),
             "mode": "offline_copy"}
    entry["route"] = {"kind": "dns_record"}
    with pytest.raises(ContractError, match="Service selector or a Gateway API route"):
        catalog_from({"version": 1, "applications": [entry]},
                     route_for=lambda spec: FakeServiceRoute(),
                     controls_for=fake_volume_controls)[0].build(
            "tenant-a", "game", "game-sessions", None)


def redis_application(tmp_path, **changes):
    server = write(tmp_path, "redis.json", json.dumps({"host": "127.0.0.1", "port": 6379,
                                                       "username": "sova-admin",
                                                       "password": "x"}))
    entry = {"servers": {"source": server, "target": server}, "prefixes": ["board:"],
             "writers": ["board-app", "board-batch"], "app_user": "board-app",
             "admin_user": "sova-admin", "sets": {"sessions": "board:session:"},
             "routes": ["src", "tgt"]}
    app = {"tenant_id": "tenant-a", "application_id": "board", "adapter_id": "redis-keyspace",
           "engine": "redis", "mode": "offline_copy", "redis": entry,
           "route": {"kind": "kubernetes_service", "registration_file": "unused",
                     "service": "board", "sides": {}}}
    for key, value in changes.items():
        (entry if key.startswith("redis.") else app)[key.removeprefix("redis.")] = value
    return app


def test_a_redis_keyspace_gets_the_keyspace_adapter(tmp_path):
    pytest.importorskip("redis")
    catalog, close = catalog_from({"version": 1, "applications": [redis_application(tmp_path)]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "board", "redis-keyspace", None)
    assert type(execution.adapter).__name__ == "RedisKeyspaceHandoffAdapter"
    close()


def test_a_writers_own_redis_login_is_tried_once(tmp_path):
    """redis-py retries a refused login (an AuthenticationError is a
    ConnectionError) with growing waits; a probe's attempt must not."""
    pytest.importorskip("redis")
    from sova.service.redis_execution import redis_resolver

    app = redis_application(tmp_path)
    resolve, close = redis_resolver(app, route_for=lambda spec: FakeRoute())
    client = resolve("tenant-a", "board")["connect_as"]("source", "board-app")
    assert client.get_retry().get_retries() == 0
    close()


def test_a_redis_entry_registers_each_sides_replicas(tmp_path):
    pytest.importorskip("redis")
    replica = write(tmp_path, "redis-replica.json", json.dumps({
        "host": "10.0.0.7", "port": 6379, "username": "sova-admin", "password": "x"}))
    app = redis_application(tmp_path, **{"redis.replicas": {"source": [replica, replica]}})
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    adapter = catalog.build("tenant-a", "board", "redis-keyspace", None).adapter
    nodes = adapter._nodes("source")
    assert [name for name, _ in nodes] == ["primary", "replica-0", "replica-1"]
    assert nodes[1][1].connection_pool.connection_kwargs["host"] == "10.0.0.7"
    assert [name for name, _ in adapter._nodes("target")] == ["primary"]
    close()


def test_a_redis_side_may_be_a_cluster(tmp_path):
    pytest.importorskip("redis")
    from sova.service.redis_execution import _server, redis_resolver

    node = write(tmp_path, "cluster.json", json.dumps({
        "host": "10.0.0.5", "port": 6379, "username": "sova-admin", "password": "x",
        "cluster": True}))
    assert _server(node)["cluster"] is True
    app = redis_application(tmp_path)
    app["redis"]["servers"]["source"] = node
    resolve, close = redis_resolver(app, route_for=lambda spec: FakeRoute())
    login = resolve("tenant-a", "board")["connect_as"]("source", "board-app",
                                                         node=("10.0.0.7", 6380))
    kwargs = login.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["username"]) == (
        "10.0.0.7", 6380, "board-app") and "cluster" not in kwargs
    close()


def test_a_redis_entry_names_each_sides_sentinels(tmp_path):
    pytest.importorskip("redis")
    watcher = write(tmp_path, "sentinel.json", json.dumps({"host": "10.0.0.9", "port": 26379}))
    app = redis_application(tmp_path, **{"redis.sentinels": {
        "source": {"servers": [watcher], "master": "board"}}})
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    adapter = catalog.build("tenant-a", "board", "redis-keyspace", None).adapter
    clients, master = adapter.sentinel("source")
    assert master == "board" and clients[0].connection_pool.connection_kwargs["port"] == 26379
    assert adapter.sentinel("target") is None
    close()


@pytest.mark.parametrize("changes", [
    {"redis.prefixes": ["board:*:sessions"]},  # a glob, not a prefix
    {"redis.app_user": "someone-else"},
    {"redis.routes": ["src"]},
    {"redis.unexpected": True},
    {"redis.replicas": {"source": "one.json"}},  # a list per side
    {"redis.replicas": {"elsewhere": []}},
    {"redis.sentinels": {"source": {"servers": [], "master": "board"}}},
    {"redis.sentinels": {"source": {"servers": ["s.json"]}}},
    {"mode": "logical_replication"},
])
def test_a_redis_entry_that_does_not_add_up_is_refused(tmp_path, changes):
    pytest.importorskip("redis")
    with pytest.raises(ContractError):
        catalog = catalog_from({"version": 1, "applications": [
            redis_application(tmp_path, **changes)]}, route_for=lambda spec: FakeRoute())[0]
        catalog.build("tenant-a", "board", "redis-keyspace", None)


def test_a_mysql_replication_may_take_its_snapshot_without_the_global_lock(tmp_path):
    secret = write(tmp_path, "repl", "replication-secret")
    spec = {"source_endpoint": ["orders-db.internal", 3306], "account": "sova_repl@%",
            "password_file": secret, "snapshot": "lockless"}
    catalog, close = catalog_from(
        {"version": 1, "applications": [mysql_application(tmp_path, mode="replication",
                                                          replication=spec)]},
        route_for=lambda spec: FakeRoute())
    adapter = catalog.build("tenant-a", "orders", "mysql-offline-copy", None).adapter
    assert adapter.snapshot_mode == "lockless"
    close()
    spec["snapshot"] = "whenever"
    with pytest.raises(ContractError, match="locked .* or lockless"):
        catalog_from({"version": 1, "applications": [
            mysql_application(tmp_path, mode="replication", replication=spec)]},
            route_for=lambda spec: FakeRoute())


def test_a_mysql_application_may_capture_changes_by_replication(tmp_path):
    secret = write(tmp_path, "repl", "replication-secret")
    app = mysql_application(tmp_path, mode="replication", replication={
        "source_endpoint": ["orders-db.internal", 3306], "account": "sova_repl@%",
        "password_file": secret})
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "mysql-offline-copy", None)
    assert type(execution.adapter).__name__ == "MySqlReplicationAdapter"
    assert execution.adapter.replication_password == "replication-secret"
    close()


def test_replication_without_its_settings_is_refused(tmp_path):
    app = mysql_application(tmp_path, mode="replication")
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    with pytest.raises(ContractError, match="replication settings"):
        catalog.build("tenant-a", "orders", "mysql-offline-copy", None)
    close()


def kafka_application(tmp_path, *, admin=None, **changes):
    admin = write(tmp_path, "kafka-admin.json", json.dumps(admin or {
        "bootstrap.servers": "broker:9092", "security.protocol": "SASL_SSL",
        "sasl.mechanism": "SCRAM-SHA-256", "sasl.username": "sova-admin",
        "sasl.password": "x", "ssl.ca.location": "/etc/sova/kafka-ca.pem"}))
    entry = {"clusters": {"source": admin, "target": admin},
             "topics": {"source": "orders", "target": "orders-new"},
             "producers": ["User:orders-app"], "app_producer": "User:orders-app",
             "probe_prefix": "sova-probe-orders-",
             "principal_settings": {"User:orders-app": {"source": admin, "target": admin}}}
    app = {"tenant_id": "tenant-a", "application_id": "orders", "adapter_id": "kafka-topic",
           "engine": "kafka", "mode": "offline_copy", "kafka": entry,
           "route": {"kind": "kubernetes_service", "registration_file": "unused",
                     "service": "orders", "sides": {}}}
    app.update(changes)
    return app


def test_a_kafka_topic_gets_the_topic_adapter(tmp_path):
    catalog, close = catalog_from({"version": 1, "applications": [kafka_application(tmp_path)]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "kafka-topic", None)
    assert type(execution.adapter).__name__ == "KafkaTopicHandoffAdapter"
    close()


def test_client_settings_that_load_code_are_refused_whoever_wrote_them(tmp_path):
    with pytest.raises(ContractError, match="connection settings only"):
        catalog_from({"version": 1, "applications": [kafka_application(tmp_path, admin={
            "bootstrap.servers": "broker:9092",
            "plugin.library.paths": "/tmp/evil.so"})]}, route_for=lambda spec: FakeRoute())


def test_a_rabbitmq_queue_gets_the_queue_adapter(tmp_path):
    pytest.importorskip("pika")
    broker = write(tmp_path, "rabbit.json", json.dumps({
        "http": "https://rabbit.internal:15671", "host": "rabbit.internal", "port": 5671,
        "username": "sova-admin", "password": "x"}))
    app = {"tenant_id": "tenant-a", "application_id": "orders", "adapter_id": "rabbitmq-queue",
           "engine": "rabbitmq", "mode": "offline_copy",
           "rabbitmq": {"brokers": {"source": broker, "target": broker},
                        "vhosts": {"source": "orders", "target": "orders-new"},
                        "queue": "orders-work", "exchange": "orders",
                        "routing_key": "order.created", "publishers": ["orders-app"],
                        "app_publisher": "orders-app",
                        "invariants": {"QUEUE-01": {"type": "same_count"}}},
           "route": {"kind": "kubernetes_service", "registration_file": "unused",
                     "service": "orders", "sides": {}}}
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "rabbitmq-queue", None)
    assert type(execution.adapter).__name__ == "RabbitMqQueueHandoffAdapter"
    app["rabbitmq"]["unexpected"] = True
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [app]}, route_for=lambda spec: FakeRoute())
    close()


def test_a_rabbitmq_ca_file_verifies_both_channels_and_refuses_plain_management(tmp_path):
    """AMQP and the management API both carry the administrator's credentials:
    with ``ssl_ca`` each is verified with that CA, and plain http is refused."""
    import ssl

    pytest.importorskip("pika")
    pytest.importorskip("cryptography")
    from mysql_pair_business import tls_material

    from sova.service.rabbitmq_execution import rabbitmq_resolver

    ca = write(tmp_path, "rabbit-ca.pem", tls_material("unit")["ca.pem"].decode())

    def resolved(http):
        broker = write(tmp_path, "rabbit-tls.json", json.dumps({
            "http": http, "host": "rabbit.internal", "port": 5671, "username": "sova-admin",
            "password": "x", "ssl_ca": ca}))
        app = {"tenant_id": "tenant-a", "application_id": "orders",
               "rabbitmq": {"brokers": {"source": broker, "target": broker},
                            "vhosts": {"source": "orders", "target": "orders-new"},
                            "queue": "orders-work", "exchange": "orders",
                            "routing_key": "order.created", "publishers": ["orders-app"],
                            "app_publisher": "orders-app"},
               "route": {}}
        resolve, _ = rabbitmq_resolver(app, route_for=lambda spec: FakeRoute())
        return resolve("tenant-a", "orders")

    broker = resolved("https://rabbit.internal:15671")["broker"]("target")
    context = broker["amqp"]("orders").ssl_options.context
    assert broker["verify"] is context and context.verify_mode == ssl.CERT_REQUIRED and \
        context.check_hostname
    with pytest.raises(ContractError, match="https"):
        resolved("http://rabbit.internal:15672")


def test_a_mysql_ca_file_reaches_the_driver_with_verification(tmp_path, monkeypatch):
    """PyMySQL replaces an ``ssl`` dict by one built from its ``ssl_*`` arguments:
    the CA must go in as ``ssl_ca`` or it is dropped (and the system store used)."""
    pymysql = pytest.importorskip("pymysql")
    seen = []
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: seen.append(kwargs))
    tls = write(tmp_path, "mysql-tls.json", json.dumps({
        "host": "orders-db", "port": 3306, "user": "sova-admin", "ssl_ca": "/etc/sova/ca.pem"}))
    app = mysql_application(tmp_path, databases={
        db: {"dsn_file": tls} for db in ("src", "tgt", "scratch", "other")})
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "mysql-offline-copy", None)
    execution.adapter.connect("tgt")
    assert seen[-1]["ssl_ca"] == "/etc/sova/ca.pem" and "ssl" not in seen[-1]
    assert seen[-1]["ssl_verify_cert"] is True and seen[-1]["ssl_verify_identity"] is True
    close()


def test_a_mongodb_database_gets_the_database_adapter(tmp_path):
    pytest.importorskip("pymongo")
    server = write(tmp_path, "mongo.json", json.dumps({
        "host": "orders-db", "port": 27017, "username": "sova-admin", "password": "x",
        "authSource": "admin", "tls_ca": "/etc/sova/mongo-ca.pem"}))
    app = {"tenant_id": "tenant-a", "application_id": "orders", "adapter_id": "mongodb-db",
           "engine": "mongodb", "mode": "offline_copy",
           "mongodb": {"servers": {"source": server, "target": server},
                       "databases": {"source": "orders", "target": "orders_new"},
                       "collections": ["orders"], "writers": ["orders-app@admin"],
                       "app_user": "orders-app@admin", "admin_user": "sova-admin@admin"},
           "route": {"kind": "kubernetes_service", "registration_file": "unused",
                     "service": "orders", "sides": {}}}
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "mongodb-db", None)
    assert type(execution.adapter).__name__ == "MongoDatabaseHandoffAdapter"
    app["mongodb"]["collections"] = ["orders; drop"]
    with pytest.raises(ContractError):
        catalog_from({"version": 1, "applications": [app]},
                     route_for=lambda spec: FakeRoute())[0].build(
            "tenant-a", "orders", "mongodb-db", None)
    close()


def test_a_mongodb_change_stream_mode_gets_the_change_capture_adapter(tmp_path):
    pytest.importorskip("pymongo")
    server = write(tmp_path, "mongo.json", json.dumps({
        "host": "orders-db", "port": 27017, "username": "sova-admin", "password": "x"}))
    app = {"tenant_id": "tenant-a", "application_id": "orders", "adapter_id": "mongodb-cdc",
           "engine": "mongodb", "mode": "change_stream",
           "mongodb": {"servers": {"source": server, "target": server},
                       "databases": {"source": "orders", "target": "orders_new"},
                       "collections": ["orders"], "writers": ["orders-app@admin"],
                       "app_user": "orders-app@admin", "admin_user": "sova-admin@admin",
                       "catch_up_seconds": 30},
           "route": {}}
    catalog, close = catalog_from({"version": 1, "applications": [app]},
                                  route_for=lambda spec: FakeRoute())
    adapter = catalog.build("tenant-a", "orders", "mongodb-cdc", None).adapter
    assert type(adapter).__name__ == "MongoChangeStreamHandoffAdapter"
    assert adapter.catch_up_seconds == 30
    close()
    app["mongodb"]["catch_up_seconds"] = 0
    with pytest.raises(ContractError, match="catch_up_seconds"):
        catalog_from({"version": 1, "applications": [app]},
                     route_for=lambda spec: FakeRoute())[0].build(
            "tenant-a", "orders", "mongodb-cdc", None)
    for engine, entry in (("redis", "redis"), ("rabbitmq", "rabbitmq")):  # no change log
        with pytest.raises(ContractError, match="mode offline_copy"):
            catalog_from({"version": 1, "applications": [{
                "tenant_id": "tenant-a", "application_id": "orders", "adapter_id": "x",
                "engine": engine, "mode": "change_stream", entry: {}, "route": {}}]},
                route_for=lambda spec: FakeRoute())


def test_a_resume_token_is_read_for_its_cluster_time():
    from sova.adapters.mongodb_change_stream import token_time

    # 0x82, then seconds 0x6578a1b2 and increment 3 big-endian, then the rest
    token = {"_data": "826578A1B2000000032B022C0100296E5A1004"}
    assert token_time(token) == (0x6578A1B2, 3)
    for bad in ({"_data": "8265"}, {"_data": "C3" + "0" * 30}, {"_data": "82zz" + "0" * 30},
                {"_data": 7}, {}, None, "826578A1B200000003"):
        assert token_time(bad) is None, bad


def test_a_postgres_target_may_register_its_tls_endpoint_for_transport_refusals(tmp_path):
    from mysql_pair_business import tls_material

    ca = write(tmp_path, "pg-ca.pem", tls_material("unit")["ca.pem"].decode())
    endpoint = {"host": "10.0.0.5", "port": 5432, "server_name": "orders-db", "ca_file": ca}
    catalog, close = catalog_from(
        {"version": 1, "applications": [application(tmp_path,
                                                    tls_endpoints={"tgt": endpoint})]},
        route_for=lambda spec: FakeRoute())
    execution = catalog.build("tenant-a", "orders", "postgres-offline-copy", None)
    close()
    assert execution.adapter.adapter_id == "postgres-offline-copy"
    for bad in ({"elsewhere": endpoint}, {"tgt": {**endpoint, "verify": False}},
                {"tgt": {k: v for k, v in endpoint.items() if k != "ca_file"}}):
        with pytest.raises(ContractError):
            catalog_from({"version": 1, "applications": [application(tmp_path,
                                                                      tls_endpoints=bad)]},
                         route_for=lambda spec: FakeRoute())
    with pytest.raises(ContractError):  # MySQL units do not take one yet
        catalog_from({"version": 1, "applications": [mysql_application(
            tmp_path, tls_endpoints={"tgt": endpoint})]}, route_for=lambda spec: FakeRoute())


def test_controls_a_units_probes_cannot_measure_fail_rather_than_pass():
    from sova.service.postgres_execution import data_measure

    class Probes:
        def transport_refusals(self, side):
            return True, {"probe": "transport_refusals", "side": side}

    measure = data_measure(Probes(), other_db="other", scratch_db="scratch")
    passed, observation = measure({"kind": "target_control_test",
                                   "id": "security.refuse_unverified_transport"}, {})
    assert passed and observation["side"] == "target"
    passed, observation = measure({"kind": "target_control_test",
                                   "id": "security.deny_anonymous_data_access"}, {})
    assert not passed and "cannot be measured" in observation["reason"]


def test_any_entry_may_have_its_secrets_measured_against_other_workloads(tmp_path):
    from sova.service.secret_access import CONTROL

    class Probes:
        def cross_workload_secret_denied(self):
            return True, {"probe": "cross_workload_secret_denied"}

    seen = []

    def secret_access_for(spec):
        seen.append(spec)
        return Probes()

    access = {"registration_file": "unused", "secrets": ["orders-db"],
              "others": [["orders", "billing"]]}
    catalog, close = catalog_from(
        {"version": 1, "applications": [
            application(tmp_path, secret_access=access),
            application(tmp_path, application_id="billing")]},
        route_for=lambda spec: FakeRoute(), secret_access_for=secret_access_for)
    assert seen == [access]
    measured = catalog.build("tenant-a", "orders", "postgres-offline-copy", None).measure(
        {"kind": "target_control_test", "id": CONTROL}, {})
    assert measured == (True, {"probe": "cross_workload_secret_denied"})
    unregistered = catalog.build("tenant-a", "billing", "postgres-offline-copy", None).measure(
        {"kind": "target_control_test", "id": CONTROL}, {})
    assert unregistered[0] is False  # required but not registered: never a pass
    close()


@pytest.mark.parametrize("spec", [
    {"registration_file": "x", "secrets": ["s"]},
    {"registration_file": "x", "secrets": ["s"], "others": [["only-namespace"]]},
    {"registration_file": "x", "secrets": ["s"], "others": [["a", "b"]], "verify": False}])
def test_a_secret_access_entry_that_does_not_add_up_is_refused(spec):
    from sova.service.secret_access import secret_access_probes

    with pytest.raises(ContractError):
        secret_access_probes(spec)
