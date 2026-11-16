"""A Redis-protocol keyspace over verified TLS, through the worker's own
execution settings: the settings file names the CA, and what the catalog
builds from it is a connection that verifies the server's certificate and
host name - measured by the transport probe and used for a whole handoff."""

import json

from adapter_conformance import apply, readback
from mysql_pair_business import tls_material
import pytest
from redis_business import ADMIN
from redis_world import Route

from sova.adapters.redis_keyspace_handoff import RedisKeyspaceHandoffAdapter
from sova.adapters.redis_keyspace_probes import RedisKeyspaceProbes
from sova.domain.contracts import EffectStatus
from sova.service.redis_execution import _server

pytestmark = [pytest.mark.redis, pytest.mark.kubernetes]


def settings(tmp_path, b, side, *, tls):
    path = tmp_path / f"{side}-{'tls' if tls else 'plain'}.json"
    path.write_text(json.dumps({
        "host": "127.0.0.1", "port": (b["tls_ports"] if tls else b["ports"])[side],
        "username": ADMIN[0], "password": ADMIN[1], **({"ssl_ca": b["ca_file"]} if tls else {})}))
    return _server(path)


def test_the_catalogs_tls_settings_verify_and_carry_a_handoff(redis_business, tmp_path):
    b, prefix, users = redis_business, redis_business["prefix"], redis_business["users"]
    redis = b["redis"]
    clients = {side: redis.Redis(**settings(tmp_path, b, side, tls=True))
               for side in ("source", "target")}
    plain = {side: redis.Redis(**settings(tmp_path, b, side, tls=False))
             for side in ("source", "target")}

    def probes(connect):
        return RedisKeyspaceProbes(connect=connect, connect_as=lambda side, name: None,
                                   writers=(users["app"],), app_user=users["app"],
                                   sets={"sessions": prefix + "session:"})

    passed, observation = probes(lambda side: clients[side]).transport_encrypted("target")
    assert passed and observation["client_verification"] == "verify-identity", observation
    assert not probes(lambda side: plain[side]).transport_encrypted("target")[0]
    route = Route("source")
    adapter = RedisKeyspaceHandoffAdapter(
        "redis-keyspace-handoff", connect=lambda side: clients[side],
        prefixes=(prefix + "*",), writers=(users["app"], users["batch"]),
        app_user=users["app"], admin_user=ADMIN[0], route=route, routes=("source", "target"))
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "verify_final_state", "activate_target", "commit_route"):
        apply(adapter, action)
        assert readback(adapter, action) == EffectStatus.APPLIED, action
    other_ca = tmp_path / "other-ca.pem"  # a CA that does not vouch for the server
    other_ca.write_bytes(tls_material("elsewhere")["ca.pem"])
    wrong = tmp_path / "wrong-ca.json"
    wrong.write_text(json.dumps({"host": "127.0.0.1", "port": b["tls_ports"]["target"],
                                 "username": ADMIN[0], "password": ADMIN[1],
                                 "ssl_ca": str(other_ca)}))
    with pytest.raises(redis.exceptions.ConnectionError):
        redis.Redis(**_server(wrong)).ping()
