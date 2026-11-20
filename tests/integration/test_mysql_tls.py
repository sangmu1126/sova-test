"""MySQL over verified TLS, measured: the transport probe passes only on a
session that is encrypted with the server's certificate and host name
checked, and a replication channel that requires TLS verifies the source's
certificate - a CA that does not vouch for it keeps the sync from starting."""

from datetime import UTC, datetime
import time

from adapter_conformance import apply, command, readback
from mysql_business import PROBE_INSERT, SCHEMA, TABLES
from mysql_pair_business import REPLICATION
from mysql_pair_world import MySqlPairWorld
import pytest
from tls_checks import endpoint, identity_held, summary

from sova.adapters.mysql_handoff_probes import MySqlHandoffProbes
from sova.adapters.mysql_replication import MySqlReplicationAdapter
from sova.adapters.tls_refusal import mysql_plaintext, transport_refusals
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.mysql, pytest.mark.kubernetes]


def probes(b, admin_connect):
    names = b["names"]
    return MySqlHandoffProbes(connect_as=b["connect_as"], admin_connect=admin_connect,
                              source_db=names["src"], target_db=names["tgt"],
                              app_account=b["app"], tables=TABLES, probe_insert=PROBE_INSERT)


def test_the_transport_probe_passes_only_on_verified_tls(mysql_pair_business):
    b = mysql_pair_business

    def verified(database):
        return b["pymysql"].connect(**b["admins"][b["side_of"](database)],
                                    ssl_ca=b["ca_file"], ssl_verify_cert=True,
                                    ssl_verify_identity=True, autocommit=False)

    passed, observation = probes(b, verified).transport_encrypted("target")
    assert passed and observation["tls_version"].startswith("TLSv1."), observation
    assert observation["client_verification"] == "verify-identity"
    # The server offers TLS, so even a client that checks nothing may end up
    # encrypted; without a verified certificate and host it does not pass.
    passed, unchecked = probes(b, b["connect"]).transport_encrypted("target")
    assert not passed and unchecked["client_verification"] == "none", unchecked


def adapter(world, ca_path):
    b = world.b
    return MySqlReplicationAdapter(
        "mysql-replication", connect=b["connect"], source_db=world.names["src"],
        target_db=world.names["tgt"], app_account=b["app"], tables=TABLES,
        schema_sql=SCHEMA, route=world._route,
        writer_accounts=tuple(world.writers.values()), source_endpoint=b["source_endpoint"],
        replication_account=f"{REPLICATION[0]}@%", replication_password=REPLICATION[1],
        replication_tls=True, replication_ca_path=ca_path, catch_up_seconds=60)


def test_replication_requiring_tls_verifies_the_source(mysql_pair_business):
    world = MySqlPairWorld(mysql_pair_business)
    trusted = adapter(world, "/etc/mysql-tls/ca.pem")
    for action in ("stage_target", "start_sync"):
        apply(trusted, action)
    observation = trusted.readback(command(trusted.adapter_id, "start_sync"), datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    status = trusted._status()
    assert status["Source_SSL_Allowed"] == "Yes" and \
        status["Source_SSL_Verify_Server_Cert"] == "Yes"
    world.business_write(3)
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(trusted, action)
    assert readback(trusted, "verify_final_state") == EffectStatus.APPLIED


def test_a_ca_that_does_not_vouch_for_the_source_keeps_the_sync_from_starting(
        mysql_pair_business):
    world = MySqlPairWorld(mysql_pair_business)
    untrusted = adapter(world, "/etc/pki/tls/certs/ca-bundle.crt")  # the image's public CAs
    apply(untrusted, "stage_target")
    apply(untrusted, "start_sync")
    deadline = time.monotonic() + 30
    while untrusted._status()["Replica_IO_Running"] == "Connecting" and \
            time.monotonic() < deadline:
        time.sleep(1)
    assert readback(untrusted, "start_sync") == EffectStatus.UNKNOWN
    assert untrusted._status()["Last_IO_Error"]
    untrusted.release_replication()


def test_a_server_offering_tls_takes_plaintext_until_it_requires_secure_transport(
        mysql_pair_business):
    """TLS offered is not plaintext refused: the login without TLS goes on to
    authentication. With ``require_secure_transport`` the server answers 3159
    before any password is asked for."""
    b = mysql_pair_business
    admin = dict(b["admins"]["target"])
    check = endpoint(admin["port"], b["ca_file"], preamble="mysql")
    user = b["app"].rsplit("@", 1)[0]
    passed, observation = transport_refusals(check, plaintext=mysql_plaintext(user))
    assert not passed and observation["accepted"] == ["plaintext"], summary(observation)
    assert identity_held(observation), summary(observation)
    secured = dict(admin, ssl={"ca": b["ca_file"]}, ssl_verify_cert=True,
                   ssl_verify_identity=True)
    with b["pymysql"].connect(**admin) as conn, conn.cursor() as cur:
        cur.execute("SET GLOBAL require_secure_transport = ON")
    try:
        passed, observation = transport_refusals(check, plaintext=mysql_plaintext(user))
    finally:  # only a TLS session may switch it back now
        with b["pymysql"].connect(**secured) as conn, conn.cursor() as cur:
            cur.execute("SET GLOBAL require_secure_transport = OFF")
    assert passed, summary(observation)
    assert observation["checks"]["plaintext"]["reason"] == "server_requires_tls"

