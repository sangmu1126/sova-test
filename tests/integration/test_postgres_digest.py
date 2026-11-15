"""The server-side content digest is the client-side one, value for value.

Evidence produced before and after ADR-0004 stays comparable only if the
digest computed by PostgreSQL equals the one computed from streamed rows, for
every kind of value a row's text form can take.
"""

from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from sova.adapters.postgres_digest import client_digest, table_digest

pytestmark = pytest.mark.postgres


@pytest.fixture
def database(postgres_database):
    dsn = postgres_database[0]
    name = f"digest_{uuid4().hex[:10]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield lambda: psycopg.connect(dsn, dbname=name)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def client_side(conn, table):
    with conn.transaction():
        for setting in ("SET LOCAL TimeZone TO 'UTC'", "SET LOCAL DateStyle TO 'ISO, YMD'",
                        "SET LOCAL IntervalStyle TO 'postgres'",
                        "SET LOCAL extra_float_digits TO 3", "SET LOCAL bytea_output TO 'hex'"):
            conn.execute(setting)
        return client_digest(text for (text,) in conn.execute(
            sql.SQL("SELECT t::text FROM {} t").format(sql.Identifier(table))))


def test_server_and_client_digests_agree_on_every_kind_of_value(database):
    with database() as conn:
        conn.execute("CREATE TABLE mixed (id int, label text, amount numeric, at timestamptz, "
                     "span interval, ratio float8, raw bytea, tags text[], doc jsonb)")
        conn.execute("""INSERT INTO mixed VALUES
            (1, 'plain', 10.50, '2026-11-15 09:00+09', '1 day 2 hours', 0.1, '\\x00ff', '{a,b}',
             '{"k": [1, 2]}'),
            (2, '한글 é 😀', -0.000001, NULL, NULL, 1e300, NULL, NULL, 'null'),
            (2, '한글 é 😀', -0.000001, NULL, NULL, 1e300, NULL, NULL, 'null'),
            (3, NULL, NULL, '1970-01-01 00:00Z', '-3 seconds', 'NaN', '\\x', '{}', '[]'),
            (4, E'tab\\tand\\nnewline', 1e20, 'infinity', '0', '-0', '\\xdeadbeef', '{"x y"}',
             '{"a": "b"}')""")
        conn.execute("CREATE TABLE empty_table (id int)")
        conn.commit()
        for table in ("mixed", "empty_table"):
            server = table_digest(conn, table)
            assert server == client_side(conn, table), table
            # The fallback above the size limit is the same digest.
            assert table_digest(conn, table, server_limit_bytes=0) == server
        assert table_digest(conn, "mixed")["rows"] == 5  # duplicates count


def test_session_settings_cannot_change_the_digest(database):
    with database() as conn:
        conn.execute("CREATE TABLE times (at timestamptz, f float8)")
        conn.execute("INSERT INTO times VALUES ('2026-11-15 00:00Z', 0.1)")
        conn.commit()
        conn.execute("SET TimeZone TO 'Asia/Seoul'")
        conn.execute("SET extra_float_digits TO 0")
        seoul = table_digest(conn, "times")
        conn.execute("SET TimeZone TO 'America/Los_Angeles'")
        conn.execute("SET DateStyle TO 'SQL, DMY'")
        assert table_digest(conn, "times") == seoul
