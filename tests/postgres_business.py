"""Two real PostgreSQL business databases for handoff tests: an order schema,
an application role and a second writer path (a batch job), with business
history already written by the application before any handoff starts."""

from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

TABLES = (("orders", "id"), ("work_items", "id"), ("idempotency_results", "customer"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id bigint PRIMARY KEY, customer text NOT NULL,
  idempotency_key text NOT NULL, amount numeric NOT NULL, UNIQUE (customer, idempotency_key));
CREATE TABLE IF NOT EXISTS work_items (id bigint PRIMARY KEY, order_id bigint NOT NULL,
  state text NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_results (customer text NOT NULL,
  idempotency_key text NOT NULL, result text NOT NULL,
  PRIMARY KEY (customer, idempotency_key));
CREATE SEQUENCE IF NOT EXISTS order_number_seq;
"""
PROBE_INSERT = ("INSERT INTO orders (id, customer, idempotency_key, amount) "
                "VALUES (-1, 'sova-probe', 'sova-probe', 0)")


class Route:
    def __init__(self, database):
        self.database = database

    def get(self):
        return self.database

    def set(self, database):
        self.database = database


def prepare(names, app, batch, connect, connect_as, *, schema_on):
    """The order schema, the writers' grants on the source, and business history
    written by the application before any handoff starts."""
    for name in schema_on:
        with connect(name) as conn:
            conn.execute(SCHEMA)
    with connect(names["src"]) as conn:
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                             "public TO {}").format(sql.Identifier(app)))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE order_number_seq TO {}").format(
            sql.Identifier(app)))
        conn.execute(sql.SQL("GRANT INSERT ON orders TO {}").format(sql.Identifier(batch)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(names["src"]), sql.Identifier(batch)))  # its own, not PUBLIC's
    with connect_as(names["src"], app) as conn:  # business before the handoff
        for i in range(1, 51):
            conn.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                         (i, f"customer-{i % 7}", f"key-{i}", i * 10))
            conn.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                         (f"customer-{i % 7}", f"key-{i}"))
            conn.execute("INSERT INTO work_items VALUES (%s, %s, 'done')", (i, i))
        for _ in range(10):  # order numbers already handed out to customers
            conn.execute("SELECT nextval('order_number_seq')")


@pytest.fixture
def business(postgres_database):
    """Source, target, scratch and another tenant's database, plus the app role."""
    dsn, _ = postgres_database
    suffix = uuid4().hex[:10]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    app = "orders_app_" + suffix
    batch = "orders_batch_" + suffix  # a second writer path: a nightly batch job
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(app)))
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(batch)))
        for name in names.values():
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        for zoned in (names["tgt"], names["src"]):
            admin.execute(sql.SQL("COMMENT ON DATABASE {} IS 'approved-zone-a'").format(
                sql.Identifier(zoned)))
        admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(names["other"])))

    def connect(database):
        return psycopg.connect(dsn, dbname=database)

    def connect_as(database, role):
        return psycopg.connect(dsn, dbname=database, user=role)

    prepare(names, app, batch, connect, connect_as,
            schema_on=(names["src"], names["tgt"], names["scratch"]))
    yield dict(names=names, app=app, batch=batch, connect=connect, connect_as=connect_as,
               dsn=dsn)
