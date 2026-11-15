"""How many database connections a cross-cloud handoff opens while writes are
paused, and how long opening them takes (ADR-0004 follow-up).

Every ``psycopg.connect`` is wrapped: its start, the server it reaches and the
time until the connection is usable. Connections that start between the
observed pause of admission and the observed activation are counted, by
server: the two managed servers (remote; split into the platform's admin
connections and probes acting as a writer role, which must stay fresh to test
access) and the control plane's own store (local). A measurement, not a
claim; opt-in with ``SOVA_TEST_PERF=1``.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import os
import time

from handoff_drive import run_handoff
import psycopg
import pytest

from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.perf, pytest.mark.postgres]
ROWS = int(os.environ.get("SOVA_TEST_CONNECTION_ROWS", "10000"))


@pytest.fixture(autouse=True)
def opt_in():
    if os.environ.get("SOVA_TEST_PERF") != "1":
        pytest.skip("SOVA_TEST_PERF=1 runs the measurements")


def managed_profile(data):
    availability = data["spec"]["availability"]
    availability["max_write_admission_pause_ms"] = 60000
    availability["max_client_completion_wait_ms"] = 120000


def test_profile_connections_inside_the_paused_window(postgres_database, cross_cloud_business,
                                                      monkeypatch):
    b = cross_cloud_business
    with b["connect"](b["names"]["src"]) as conn:
        conn.execute("INSERT INTO orders SELECT g, 'customer-' || g %% 97, 'key-' || g, g "
                     "FROM generate_series(1000, %s) g", (999 + ROWS,))
        conn.execute("INSERT INTO idempotency_results SELECT 'customer-' || g %% 97, "
                     "'key-' || g, 'confirmed' FROM generate_series(1000, %s) g", (999 + ROWS,))
        conn.execute("INSERT INTO work_items SELECT g, g, 'done' FROM generate_series(1000, %s) g",
                     (999 + ROWS,))
    local = postgres_database[0]
    remote = {b["servers"][b["names"]["src"]]: "source-cloud",
              b["servers"][b["names"]["tgt"]]: "target-cloud"}
    opened, real = [], psycopg.connect

    def connect(conninfo="", **kwargs):
        started, wall = time.monotonic(), datetime.now(UTC)
        conn = real(conninfo, **kwargs)
        where = remote.get(conninfo) or ("control-plane" if conninfo == local else "other")
        if where.endswith("-cloud"):  # a probe acting as a writer role, or the platform's admin
            where += ":role" if kwargs.get("user") else ":admin"
        opened.append((wall, where, (time.monotonic() - started) * 1000))
        return conn

    monkeypatch.setattr(psycopg, "connect", connect)
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), b,
                    adjust=managed_profile)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, x["record"].reason
    start, end = x["observed"]["pause_admission"], x["observed"]["activate_target"]
    inside = [(where, ms) for wall, where, ms in opened if start <= wall <= end]
    count = Counter(where for where, _ in inside)
    spent = {where: round(sum(ms for w, ms in inside if w == where)) for where in count}
    pause = round((end - start).total_seconds() * 1000)
    line = json.dumps({"direction": b["direction"], "rows": ROWS, "pause_ms": pause,
                       "connections": dict(count), "connect_ms": spent,
                       "median_connect_ms": {w: round(sorted(ms for x_, ms in inside
                                                             if x_ == w)[count[w] // 2])
                                             for w in count}})
    print(line)
    out = os.environ.get("SOVA_TEST_PERF_OUT")
    if out:
        with open(out, "a") as handle:
            handle.write(line + "\n")
