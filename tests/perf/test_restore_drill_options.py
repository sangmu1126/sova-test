"""How long a restore drill of the target takes, done two ways (ADR-0004 input).

* ``client``: the reference probe (``restore_drill``): every target table is
  read through this process into an empty scratch database and compared row
  by row - the data crosses the network twice, and once more to compare.
* ``server``: the copy stays inside the target server (``CREATE DATABASE ...
  TEMPLATE``), and each table is compared by a digest computed on the server;
  only the digests travel.

A measurement, not a claim. Opt-in (``SOVA_TEST_PERF=1``) with a cloud lease;
the target is the managed server of the cross-cloud world (Cloud SQL or RDS).
"""

import hashlib
import json
import os
import time
from uuid import uuid4

from adapter_conformance import SEQUENCES, apply
from postgres_business import TABLES
from postgres_world import PostgresWorld
from psycopg import sql
import psycopg
import pytest

from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes

pytestmark = [pytest.mark.perf, pytest.mark.postgres]
ROWS = int(os.environ.get("SOVA_TEST_DRILL_ROWS", "10000"))


@pytest.fixture(autouse=True)
def opt_in():
    if os.environ.get("SOVA_TEST_PERF") != "1":
        pytest.skip("SOVA_TEST_PERF=1 runs the measurements")


def table_digest(conn, table, key):
    row = conn.execute(sql.SQL(
        "SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t.{}), '')) "
        "FROM {} t").format(sql.Identifier(key), sql.Identifier(table))).fetchone()
    return row[0], row[1]


def server_side_drill(business):
    """Clone the target inside its server and compare digests computed there."""
    target = business["names"]["tgt"]
    clone = f"drill_{uuid4().hex[:10]}"
    dsn = business["servers"][target]
    with psycopg.connect(dsn, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(clone), sql.Identifier(target)))
    try:
        with psycopg.connect(dsn, dbname=target) as original, \
                psycopg.connect(dsn, dbname=clone) as copy:
            for table, key in TABLES:
                assert table_digest(original, table, key) == table_digest(copy, table, key)
    finally:
        with psycopg.connect(dsn, dbname="postgres", autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(clone)))


def test_measure_restore_drill_client_and_server_side(cross_cloud_business):
    b = cross_cloud_business
    with b["connect"](b["names"]["src"]) as conn:
        conn.execute("INSERT INTO orders SELECT g, 'customer-' || g %% 97, 'key-' || g, g "
                     "FROM generate_series(1000, %s) g", (999 + ROWS,))
        conn.execute("INSERT INTO idempotency_results SELECT 'customer-' || g %% 97, "
                     "'key-' || g, 'confirmed' FROM generate_series(1000, %s) g", (999 + ROWS,))
        conn.execute("INSERT INTO work_items SELECT g, g, 'done' FROM generate_series(1000, %s) g",
                     (999 + ROWS,))
    world = PostgresWorld(b)
    for action in SEQUENCES["offline-copy"][:4]:  # through copy_content: the final data
        apply(world.adapter(), action)
    probes = PostgresHandoffProbes(
        connect_as=b["connect_as"], admin_connect=b["connect"], source_db=b["names"]["src"],
        target_db=b["names"]["tgt"], app_role=b["app"], tables=TABLES, probe_insert="",
        writer_roles=(b["app"], b["batch"]))
    times = {"client": [], "server": []}
    for _ in range(3):
        started = time.monotonic()
        passed, observation = probes.restore_drill(b["names"]["scratch"])
        times["client"].append(round((time.monotonic() - started) * 1000))
        assert passed, observation
        started = time.monotonic()
        server_side_drill(b)
        times["server"].append(round((time.monotonic() - started) * 1000))
    line = json.dumps({"direction": b["direction"], "rows": ROWS, **times,
                       "digest": hashlib.sha256(json.dumps(times).encode()).hexdigest()[:8]})
    print(line)
    out = os.environ.get("SOVA_TEST_PERF_OUT")
    if out:
        with open(out, "a") as handle:
            handle.write(line + "\n")
