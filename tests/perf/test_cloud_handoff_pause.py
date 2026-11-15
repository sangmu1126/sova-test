"""Write-admission pause of real-cloud handoffs, repeated, by path and size.

A measurement, not a claim: one small managed instance per provider (RDS
db.t4g.micro, Cloud SQL db-f1-micro on a shared core), the reference adapter,
this machine in between. Opt-in with ``SOVA_TEST_PERF=1`` and a cloud lease
(``cloud-env.sh env``). ``SOVA_TEST_CLOUD_PERF_REPEATS`` (default 5) runs per
path and size; ``SOVA_TEST_CLOUD_PERF_SIZES`` (default ``0,10000``) extra
orders on top of the base business data. Each run appends one JSON line to
``SOVA_TEST_PERF_OUT`` (default: stdout only), like the local measurement.
"""

from datetime import UTC, datetime, timedelta
import json
import os

from handoff_drive import run_handoff, without_transport_control
from managed_postgres import managed_business
import pytest

from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.perf, pytest.mark.postgres]
REPEATS = int(os.environ.get("SOVA_TEST_CLOUD_PERF_REPEATS", "5"))
SIZES = tuple(int(n) for n in os.environ.get("SOVA_TEST_CLOUD_PERF_SIZES", "0,10000").split(","))
PATHS = ("rds-to-onprem", "cloudsql-to-onprem", "cloudsql-to-onprem-cdc", "aws-to-gcp",
         "gcp-to-aws")
STEPS = ("fence_source_writes", "copy_content", "apply_final_changes", "verify_final_state",
         "activate_target", "commit_route")


@pytest.fixture(autouse=True)
def opt_in():
    if os.environ.get("SOVA_TEST_PERF") != "1":
        pytest.skip("SOVA_TEST_PERF=1 runs the handoff measurements")


def managed_profile(data):
    availability = data["spec"]["availability"]
    availability["max_write_admission_pause_ms"] = 60000
    availability["max_client_completion_wait_ms"] = 120000


def business_for(path, request, postgres_database):
    if path == "rds-to-onprem":
        return request.getfixturevalue("cloud_business"), without_transport_control
    if path == "cloudsql-to-onprem":
        return request.getfixturevalue("cloudsql_business"), without_transport_control
    if path == "cloudsql-to-onprem-cdc":
        return request.getfixturevalue("cloudsql_cdc"), without_transport_control
    aws, cloudsql = request.getfixturevalue("aws"), request.getfixturevalue("cloudsql")
    rds, password = os.environ["SOVA_TEST_RDS_DSN"], os.environ["SOVA_TEST_RDS_ROLE_PASSWORD"]
    assert f"sova-test-pg-{aws['run']}." in rds
    source, target = ((rds, cloudsql["dsn"]) if path == "aws-to-gcp"
                      else (cloudsql["dsn"], rds))
    return (managed_business(postgres_database[0], source, password, target=target),
            managed_profile)


def grow(business, rows):
    if not rows:
        return
    with business["connect"](business["names"]["src"]) as conn:
        conn.execute("INSERT INTO orders SELECT g, 'customer-' || g %% 97, 'key-' || g, g "
                     "FROM generate_series(1000, %s) g", (999 + rows,))
        conn.execute("INSERT INTO idempotency_results SELECT 'customer-' || g %% 97, 'key-' || g, "
                     "'confirmed' FROM generate_series(1000, %s) g", (999 + rows,))
        conn.execute("INSERT INTO work_items SELECT g, g, 'done' FROM generate_series(1000, %s) g",
                     (999 + rows,))
        conn.execute("ANALYZE")


@pytest.mark.parametrize("repeat", range(1, REPEATS + 1))
@pytest.mark.parametrize("rows", SIZES)
@pytest.mark.parametrize("path", PATHS)
def test_measure_cloud_write_admission_pause(request, postgres_database, path, rows, repeat):
    business, adjust = business_for(path, request, postgres_database)
    grow(business, rows)
    cdc = path.endswith("-cdc")
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), business,
                    adjust=adjust, strategy="snapshot-cdc" if cdc else "offline-copy", cdc=cdc)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["observed"].get("uncertain"), x["observed"].get("failed"))
    observed = x["observed"]
    start = observed["pause_admission"]

    def since(step):
        return round((observed[step] - start).total_seconds() * 1000)

    line = json.dumps({"path": path, "rows": rows, "repeat": repeat,
                       "pause_ms": since("activate_target"),
                       "pause_to_route_ms": since("commit_route"),
                       "steps_ms": {s: since(s) for s in STEPS if s in observed}})
    print(line)
    out = os.environ.get("SOVA_TEST_PERF_OUT")
    if out:
        with open(out, "a") as handle:
            handle.write(line + "\n")
