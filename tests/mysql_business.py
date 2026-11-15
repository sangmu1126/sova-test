"""Real MySQL business databases for handoff tests: the order schema with a
trigger and an AUTO_INCREMENT counter (MySQL's sequence), an application
account and a second writer path (a batch job, table-level grant), and
business history written by the application before any handoff starts.

``SOVA_TEST_MYSQL`` names the loopback server (``host:port``) and
``SOVA_TEST_MYSQL_ROOT`` its administrative ``user:password``. Each test gets
its own databases and accounts, removed afterwards.
"""

import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

TABLES = (("orders", "id"), ("work_items", "id"), ("idempotency_results", "customer"),
          ("order_numbers", "n"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id BIGINT PRIMARY KEY, customer VARCHAR(64) NOT NULL,
  idempotency_key VARCHAR(64) NOT NULL, amount DECIMAL(12, 2) NOT NULL,
  UNIQUE KEY one_order_per_key (customer, idempotency_key)) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS work_items (id BIGINT PRIMARY KEY, order_id BIGINT NOT NULL,
  state VARCHAR(16) NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS idempotency_results (customer VARCHAR(64) NOT NULL,
  idempotency_key VARCHAR(64) NOT NULL, result VARCHAR(16) NOT NULL,
  PRIMARY KEY (customer, idempotency_key)) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS order_numbers (n BIGINT AUTO_INCREMENT PRIMARY KEY,
  issued_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) ENGINE=InnoDB;
CREATE TRIGGER IF NOT EXISTS orders_amount_not_negative BEFORE INSERT ON orders
FOR EACH ROW
BEGIN
  IF NEW.amount < 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'negative amount; refused';
  END IF;
END;
"""
PROBE_INSERT = ("INSERT INTO orders (id, customer, idempotency_key, amount) "
                "VALUES (-1, 'sova-probe', 'sova-probe', 0)")
PASSWORD = "sova-writer-test-secret"
DENIED = {1044, 1045, 1049, 1142, 1143, 1227, 1370, 3118}  # access, database, command, locked


def server():
    address, root = os.environ.get("SOVA_TEST_MYSQL"), os.environ.get("SOVA_TEST_MYSQL_ROOT")
    if not address or not root:
        pytest.skip("SOVA_TEST_MYSQL and SOVA_TEST_MYSQL_ROOT are required for MySQL tests")
    pymysql = pytest.importorskip("pymysql")
    host, port = urlsplit("//" + address).hostname, urlsplit("//" + address).port
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise pytest.UsageError("MySQL tests run only against a loopback server")
    user, password = root.split(":", 1)
    return pymysql, {"host": host, "port": port, "user": user, "password": password,
                     "connect_timeout": 5}


def denied(error):
    return getattr(error, "args", (None,))[0] in DENIED


@pytest.fixture
def mysql_business():
    pymysql, admin = server()
    suffix = uuid4().hex[:10]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    app, batch = f"orders_app_{suffix}@%", f"orders_batch_{suffix}@%"

    def connect(database):
        """The administrative connection to the server holding ``database``."""
        return pymysql.connect(**admin, autocommit=False)

    def connect_as(database, writer):
        user = writer.rsplit("@", 1)[0]
        return pymysql.connect(**{**admin, "user": user, "password": PASSWORD},
                               database=database, autocommit=False)

    def quoted(writer):
        user, host = writer.rsplit("@", 1)
        return f"'{user}'@'{host}'"

    with connect(None) as conn, conn.cursor() as cur:
        for writer in (app, batch):
            cur.execute(f"CREATE USER {quoted(writer)} IDENTIFIED BY '{PASSWORD}'")
        for key in ("src", "scratch", "other"):
            cur.execute(f"CREATE DATABASE `{names[key]}`")
        cur.execute(f"USE `{names['src']}`")
        from sova.adapters.mysql_offline_copy import statements

        for statement in statements(SCHEMA):
            cur.execute(statement)
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{names['src']}`.* TO "
                    f"{quoted(app)}")
        cur.execute(f"GRANT INSERT ON `{names['src']}`.`orders` TO {quoted(batch)}")
        conn.commit()
    with connect_as(names["src"], app) as conn, conn.cursor() as cur:
        for i in range(1, 51):  # business before the handoff
            cur.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                        (i, f"customer-{i % 7}", f"key-{i}", i * 10))
            cur.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                        (f"customer-{i % 7}", f"key-{i}"))
            cur.execute("INSERT INTO work_items VALUES (%s, %s, 'done')", (i, i))
        for _ in range(10):  # order numbers already handed out to customers
            cur.execute("INSERT INTO order_numbers () VALUES ()")
        cur.execute("DELETE FROM order_numbers WHERE n > 8")  # issued, then withdrawn
        conn.commit()
    yield {"names": names, "app": app, "batch": batch, "connect": connect,
           "connect_as": connect_as, "quoted": quoted, "admin": admin, "pymysql": pymysql}
    with connect(None) as conn, conn.cursor() as cur:
        cur.execute("XA RECOVER")
        for row in cur.fetchall():
            xid = row[3].decode() if isinstance(row[3], bytes) else row[3]
            if xid.startswith("sova-conformance-"):
                cur.execute(f"XA ROLLBACK '{xid}'")
        for database in names.values():
            cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
        for writer in (app, batch):
            cur.execute(f"DROP USER IF EXISTS {quoted(writer)}")
        conn.commit()
