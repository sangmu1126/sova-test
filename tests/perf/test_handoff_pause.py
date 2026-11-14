"""Write-admission pause of the reference PostgreSQL handoff, by size and strategy.

A measurement, not a claim: one local PostgreSQL server, both databases on it,
the reference adapter. Opt-in with ``SOVA_TEST_PERF=1``; each run appends one
JSON line to ``SOVA_TEST_PERF_OUT`` (default: stdout only). The pause is taken
from the effect ledger, from the observed pause of admission to the observed
activation of the target, exactly as the contract budget is measured.

``SOVA_TEST_PERF_SIZES`` (comma-separated row counts, default ``1000,10000``)
chooses the sizes; larger ones are opt-in because on a laptop they load the
container VM heavily.
"""

from datetime import UTC, datetime, timedelta
import json
import os

from handoff_drive import run_handoff, without_transport_control
from postgres_business import logical_replication_available
import pytest

from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.postgres, pytest.mark.perf]
SIZES = tuple(int(n) for n in os.environ.get("SOVA_TEST_PERF_SIZES", "1000,10000").split(","))


@pytest.fixture(autouse=True)
def opt_in():
    if os.environ.get("SOVA_TEST_PERF") != "1":
        pytest.skip("SOVA_TEST_PERF=1 runs the handoff measurements")


def grow(business, rows):
    """Business history of ``rows`` orders with their results and work items."""
    with business["connect"](business["names"]["src"]) as conn:
        conn.execute("INSERT INTO orders SELECT g, 'customer-' || g %% 97, 'key-' || g, g "
                     "FROM generate_series(1000, %s) g", (999 + rows,))
        conn.execute("INSERT INTO idempotency_results SELECT 'customer-' || g %% 97, 'key-' || g, "
                     "'confirmed' FROM generate_series(1000, %s) g", (999 + rows,))
        conn.execute("INSERT INTO work_items SELECT g, g, 'done' FROM generate_series(1000, %s) g",
                     (999 + rows,))
        conn.execute("ANALYZE")


# (strategy, adapter): the reference adapter applies snapshot-cdc's final
# changes by a full re-copy; the logical-replication adapter only by the
# changes still in flight.
VARIANTS = (("offline-copy", "reference"), ("snapshot-cdc", "reference"),
            ("snapshot-cdc", "logical-replication"))
STEPS = ("fence_source_writes", "copy_content", "apply_final_changes", "verify_final_state",
         "activate_target", "commit_route")


@pytest.mark.parametrize("repeat", (1, 2, 3))
@pytest.mark.parametrize("rows", SIZES)
@pytest.mark.parametrize("strategy,adapter", VARIANTS)
def test_measure_write_admission_pause(postgres_database, business, strategy, adapter, rows,
                                       repeat):
    cdc = adapter == "logical-replication"
    if cdc and not logical_replication_available(business["connect"],
                                                 business["names"]["src"]):
        pytest.skip("the PostgreSQL test server runs without wal_level=logical")
    grow(business, rows)
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), business,
                    adjust=without_transport_control, strategy=strategy, cdc=cdc)
    if cdc:
        x["adapter"].release_replication()
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["observed"].get("uncertain"), x["observed"].get("failed"))
    observed = x["observed"]
    start = observed["pause_admission"]

    def since(step):
        return round((observed[step] - start).total_seconds() * 1000)

    line = json.dumps({"strategy": strategy, "adapter": adapter, "rows": rows,
                       "repeat": repeat, "pause_ms": since("activate_target"),
                       "pause_to_route_ms": since("commit_route"),
                       "steps_ms": {s: since(s) for s in STEPS if s in observed}})
    print(line)
    if os.environ.get("SOVA_TEST_PERF_OUT"):
        with open(os.environ["SOVA_TEST_PERF_OUT"], "a", encoding="utf-8") as out:
            out.write(line + "\n")
