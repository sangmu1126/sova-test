"""MongoDB over verified TLS through the worker's own settings: what the
catalog builds from ``tls_ca`` verifies the server's certificate and host
name, the transport probe measures it, and a whole handoff runs over it."""

import json

from adapter_conformance import apply, readback
from mongodb_business import ADMIN
from mongodb_world import COLLECTIONS, Route
from mysql_pair_business import tls_material
import pytest
from tls_checks import endpoint, identity_held, summary

from sova.adapters.mongodb_handoff import MongoDatabaseHandoffAdapter
from sova.adapters.mongodb_probes import MongoDatabaseProbes
from sova.adapters.tls_refusal import MONGODB_HELLO, hello_plaintext, transport_refusals
from sova.domain.contracts import EffectStatus
from sova.service.mongodb_execution import _client_settings

pytestmark = [pytest.mark.mongodb, pytest.mark.kubernetes]


def client(b, tmp_path, side, ca):
    path = tmp_path / f"{side}-{abs(hash(ca))}.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": b["ports"][side],
                                "username": ADMIN[0], "password": ADMIN[1],
                                "authSource": "admin", **({"tls_ca": ca} if ca else {})}))
    return b["pymongo"].MongoClient(**_client_settings(path), directConnection=True,
                                    serverSelectionTimeoutMS=5000)


def test_the_catalogs_tls_settings_verify_and_carry_a_handoff(mongo_business, tmp_path):
    b = mongo_business
    tls = {side: client(b, tmp_path, side, b["ca_file"]) for side in ("source", "target")}
    plain = {side: client(b, tmp_path, side, None) for side in ("source", "target")}

    def probes(clients):
        return MongoDatabaseProbes(connect=lambda side: clients[side],
                                   connect_as=lambda side, user: None,
                                   databases={"source": b["names"]["src"],
                                              "target": b["names"]["tgt"]},
                                   collections=COLLECTIONS, writers=b["writers"],
                                   app_user=b["writers"][0])

    passed, observation = probes(tls).transport_encrypted("target")
    assert passed and observation["client_verification"] == "verify-identity", observation
    assert not probes(plain).transport_encrypted("target")[0]
    adapter = MongoDatabaseHandoffAdapter(
        "mongodb-handoff", connect=lambda side: tls[side], source_db=b["names"]["src"],
        target_db=b["names"]["tgt"], collections=COLLECTIONS, writers=b["writers"],
        app_user=b["writers"][0], admin_user=f"{ADMIN[0]}@admin",
        route=Route(b["names"]["src"]))
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "verify_final_state", "activate_target", "commit_route"):
        apply(adapter, action)
        assert readback(adapter, action) == EffectStatus.APPLIED, action
    other_ca = tmp_path / "other-ca.pem"  # a CA that does not vouch for the server
    other_ca.write_bytes(tls_material("elsewhere")["ca.pem"])
    with pytest.raises(b["pymongo"].errors.ServerSelectionTimeoutError):
        client(b, tmp_path, "target", str(other_ca)).admin.command("ping")


def test_mongodb_preferring_tls_still_takes_plaintext(mongo_servers):
    passed, observation = transport_refusals(
        endpoint(mongo_servers["ports"]["target"], mongo_servers["ca_file"]),
        plaintext=hello_plaintext(MONGODB_HELLO))
    assert not passed and observation["accepted"] == ["plaintext"]
    assert identity_held(observation), summary(observation)
