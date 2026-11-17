"""The PostgreSQL write and isolation probes need two views to agree
(ADR-0007): a writer whose login is refused but whose open session still
writes is not denied, and a server the probe cannot reach decides nothing -
neither the write denial nor the isolation of another tenant passes."""

from postgres_business import PROBE_INSERT, TABLES
import psycopg
from psycopg import sql
import pytest

from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes

pytestmark = pytest.mark.postgres


def probes(business, *, connect=None, connect_as=None):
    names = business["names"]
    return PostgresHandoffProbes(
        connect_as=connect_as or business["connect_as"],
        admin_connect=connect or business["connect"], source_db=names["src"],
        target_db=names["tgt"], app_role=business["app"], tables=TABLES,
        probe_insert=PROBE_INSERT, writer_roles=(business["app"],))


def test_a_refused_login_with_a_session_still_open_is_not_a_denial(business):
    names, app = business["names"], business["app"]
    p = probes(business)
    opened = business["connect_as"](names["src"], app)  # the application's live session
    try:
        with business["connect"](names["src"]) as admin:
            admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(app)))
        passed, observation = p.write_denied("source")
        outcome = observation["outcomes"][app]
        assert not passed and outcome["attempt"] == "connection_refused", outcome
        assert outcome["open_sessions"] >= 1 and outcome["writable_tables"], outcome
        with opened.transaction(force_rollback=True):  # what the server's view warned of
            opened.execute(PROBE_INSERT)
    finally:
        opened.close()
    # The session gone and the login still refused: its table rights remain,
    # but nothing can reach them - both views now agree.
    passed, observation = p.write_denied("source")
    assert passed and observation["outcomes"][app]["open_sessions"] == 0, observation


def test_an_unreachable_server_decides_nothing(business):
    def nowhere(*args):
        return psycopg.connect(host="127.0.0.1", port=1, connect_timeout=2, user="nobody",
                               dbname="nowhere")

    p = probes(business, connect=nowhere, connect_as=nowhere)
    passed, observation = p.write_denied("source")
    assert not passed and observation["outcomes"][business["app"]] == {
        "attempt": "undecided:unreachable"}
    passed, observation = p.cross_tenant_denied(business["names"]["other"])
    assert not passed and observation["outcome"] == "undecided:unreachable"


def test_another_tenants_database_is_refused_by_the_server(business):
    passed, observation = probes(business).cross_tenant_denied(business["names"]["other"])
    assert passed and observation == {"probe": "cross_tenant_denied",
                                      "outcome": "connection_refused",
                                      "connect_granted": False}
