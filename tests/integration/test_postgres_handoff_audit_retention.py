"""AT-37: the runtime role cannot erase or rewrite handoff records.

History, change events, frozen plans, reaffirmations and the lifecycle audit
are append-only for the role that runs handoffs. Synthetic data only.
"""

from handoff_support import admitted_handoff
import psycopg
import pytest

pytestmark = pytest.mark.postgres

APPEND_ONLY = (
    ("handoff_authority_events", "action"),
    ("change_events", "subject"),
    ("compiled_plans", "canonical"),
    ("plan_reaffirmations", "actor_id"),
    ("lifecycle_audit", "action"),
    ("change_feed_watermarks", "collector_id"),
)


@pytest.mark.parametrize("table,column", APPEND_ONLY)
def test_the_runtime_role_cannot_delete_or_rewrite_records(postgres_database, now, table,
                                                           column):
    h = admitted_handoff(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    for statement in (f"DELETE FROM sova_control.{table}",
                      f"UPDATE sova_control.{table} SET {column} = {column}",
                      f"TRUNCATE sova_control.{table}"):
        with h["connect"]() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT set_config('sova.tenant_id', %s, false)", (h["tenant"],))
            conn.execute(statement)
