"""Explicit retry metrics must retain first-attempt failures."""

import pytest
from qualification_load_client import metrics


def event(status, elapsed, phase="load"):
    return {"status": status, "elapsed_ms": elapsed, "phase": phase}


def test_successful_retry_does_not_erase_http_or_transport_errors():
    result = metrics([event(200, 10), event(503, 20), event(None, 1000),
                      event(404, 8, "recovery"), event(200, 12, "recovery")])
    assert result["status_counts"] == {"200": 2, "503": 1, "transport_error": 1, "404": 1}
    assert result["initial_attempts"] == 3 and result["initial_failures"] == 2
    assert result["recovery_attempts"] == 2
    assert result["elapsed_ms"] == {"p50": 12, "p95": 1000, "max": 1000}
    assert result["service_continuity"] == "NOT_QUALIFIED" and not result["qualification_issued"]


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf"), True])
def test_invalid_timings_cannot_be_reported_as_latency(elapsed):
    with pytest.raises(ValueError, match="durations"):
        metrics([event(200, elapsed)])


def test_empty_traffic_cannot_be_reported_as_success():
    with pytest.raises(ValueError, match="nonempty"):
        metrics([])


def test_post_handoff_probe_is_neither_initial_success_nor_recovery():
    result = metrics([event(503, 10), event(200, 10, "inline_retry"),
                      event(200, 10, "recovery"), event(200, 10, "post_handoff_probe")])
    assert result["initial_attempts"] == result["initial_failures"] == 1
    assert result["recovery_attempts"] == 2
    assert result["phase_counts"]["post_handoff_probe"] == 1
