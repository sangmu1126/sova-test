"""The MySQL offline-copy adapter against the provider-neutral conformance kit,
and the ways MySQL's grants and sessions differ from PostgreSQL's."""

from datetime import UTC, datetime

from adapter_conformance import HandoffAdapterConformance, apply, readback
from mysql_world import MySqlWorld
import pytest

from sova.adapters.mysql_offline_copy import statements
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.mysql


@pytest.fixture
def world(mysql_business):
    return MySqlWorld(mysql_business)


class TestMySqlOfflineCopyConformance(HandoffAdapterConformance):
    pass


def run(world, *actions):
    for action in actions:
        apply(world.adapter(), action)


def test_a_transaction_left_open_in_another_database_cannot_commit_after_the_fence(world):
    """A session caches its rights and is not bound to one database: an
    application session that wrote the source and moved on still holds the
    write. The fence ends every session of the writer accounts."""
    b, src = world.b, world.names["src"]
    run(world, "stage_target")
    lingering = b["connect_as"](None, b["app"])
    with lingering.cursor() as cur:
        cur.execute(f"INSERT INTO `{src}`.orders VALUES (9999, 'late', 'late-1', 1)")
    run(world, "pause_admission", "fence_source_writes")
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
    with pytest.raises(b["pymysql"].err.OperationalError):
        lingering.commit()  # its session is gone; so is the uncommitted write
    assert not world._admin(f"SELECT 1 FROM `{src}`.orders WHERE id=9999")


def test_write_access_through_a_role_keeps_the_fence_unconfirmed(world):
    """The fence revokes the writer accounts' own grants only; a role that
    still lets a writer in is reported, and removing it is the operator's call."""
    b, src = world.b, world.names["src"]
    role = "orders_writers_" + src.split("_", 1)[1]
    world._admin(f"CREATE ROLE '{role}'")
    try:
        world._admin(f"GRANT INSERT ON `{src}`.* TO '{role}'")
        world._admin(f"GRANT '{role}' TO {b['quoted'](b['batch'])}")
        run(world, "stage_target", "pause_admission", "fence_source_writes")
        observation = world.adapter().readback(
            __import__("adapter_conformance").command("mysql-offline-copy",
                                                      "fence_source_writes"),
            datetime.now(UTC))
        assert observation.status == EffectStatus.UNKNOWN
        assert any(f"{role}@%" in p for p in
                   observation.evidence["source_writes"][b["batch"]]), observation.evidence
        world._admin(f"REVOKE '{role}' FROM {b['quoted'](b['batch'])}")
        assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
    finally:
        world._admin(f"DROP ROLE IF EXISTS '{role}'")


def test_identifiers_withdrawn_on_the_source_are_never_issued_again(world):
    """The counter moved past numbers the source withdrew (9 and 10); the
    target must not issue them again after the handoff."""
    b = world.b
    run(world, "stage_target", "pause_admission", "fence_source_writes", "copy_content",
        "verify_final_state", "activate_target")
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    with b["connect_as"](world.names["tgt"], b["app"]) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO order_numbers () VALUES ()")
        cur.execute("SELECT LAST_INSERT_ID()")
        issued = cur.fetchone()[0]
        conn.commit()
    assert issued == 11


def test_a_trigger_missing_on_the_target_is_a_different_structure(world):
    run(world, "stage_target")
    assert readback(world.adapter(), "stage_target") == EffectStatus.APPLIED
    world._admin(f"DROP TRIGGER `{world.names['tgt']}`.orders_amount_not_negative")
    assert readback(world.adapter(), "stage_target") == EffectStatus.UNKNOWN
    run(world, "stage_target")  # staging again restores it
    assert readback(world.adapter(), "stage_target") == EffectStatus.APPLIED


def test_ddl_scripts_split_like_the_mysql_client():
    script = """CREATE TABLE a (x VARCHAR(9) DEFAULT 'p;q');
    CREATE TRIGGER t BEFORE INSERT ON a FOR EACH ROW BEGIN
      IF NEW.x = '' THEN SET NEW.x = CASE WHEN 1 THEN 'y' ELSE 'z' END; END IF;
      CASE WHEN NEW.x = 'q' THEN BEGIN END; ELSE SET NEW.x = 'r'; END CASE;
    END;
    -- trailing; comment
    CREATE TABLE b (y INT)"""
    parts = statements(script)
    assert len(parts) == 3 and parts[1].rstrip().endswith("END"), parts
