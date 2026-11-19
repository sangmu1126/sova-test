"""One canary schema for local regression and personal physical deployments.

Only run-owned qualification data; not a generic customer schema or migration.
"""

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
