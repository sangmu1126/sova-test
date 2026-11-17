"""The MySQL write probe needs two views to agree (ADR-0007): MySQL leaves an
open session its login after the account is locked (and its global
privileges after a REVOKE), so a refused login is a denial only when the
server lists no session of the account."""

from mysql_business import PROBE_INSERT, TABLES
import pytest

from sova.adapters.mysql_handoff_probes import MySqlHandoffProbes

pytestmark = pytest.mark.mysql


def test_a_locked_account_with_a_session_still_open_is_not_a_denial(mysql_business):
    b = mysql_business
    names, app = b["names"], b["app"]
    p = MySqlHandoffProbes(connect_as=b["connect_as"], admin_connect=b["connect"],
                           source_db=names["src"], target_db=names["src"],
                           app_account=app, tables=TABLES, probe_insert=PROBE_INSERT,
                           writer_accounts=(app,))
    opened = b["connect_as"](names["src"], app)  # the application's live session
    try:
        with b["connect"](None) as conn, conn.cursor() as cur:
            cur.execute(f"ALTER USER {b['quoted'](app)} ACCOUNT LOCK")
        passed, observation = p.write_denied("source")
        outcome = observation["outcomes"][app]
        assert not passed and outcome["attempt"] == "connection_refused", outcome
        assert outcome["open_sessions"] >= 1, outcome
        with opened.cursor() as cur:  # what the server's view warned of
            cur.execute(PROBE_INSERT)
        opened.rollback()
    finally:
        opened.close()
    passed, observation = p.write_denied("source")  # both views agree now
    assert passed and observation["outcomes"][app] == {"attempt": "connection_refused",
                                                       "open_sessions": 0}, observation
