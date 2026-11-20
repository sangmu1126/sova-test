"""Actual control-DB timestamp for synthetic evidence; no clock changes/backdating."""

from datetime import UTC, datetime
import time


def observed_database_time(connect):
    with connect() as conn:
        observed = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    deadline = time.monotonic() + 2
    while datetime.now(UTC) < observed:
        if time.monotonic() >= deadline:
            raise RuntimeError("Test host has not reached the observed database clock")
        time.sleep(0.001)
    return observed
