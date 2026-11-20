"""History cursors carry no authority and cannot change the query scope."""

import base64
import json
from datetime import UTC, datetime

import pytest

from sova.domain.contracts import ContractError
from sova.service.change_history import cursor, position


def test_cursor_round_trip_preserves_microseconds_and_scope():
    at = datetime(2026, 11, 20, 3, 2, 1, 123456, tzinfo=UTC)
    value = cursor("tenant", "app", at, "event:one")
    assert position(value, "tenant", "app") == (at, "event:one")
    assert position("", "tenant", "app") is None
    for tenant, app in (("other", "app"), ("tenant", "other")):
        with pytest.raises(ContractError, match="cursor"):
            position(value, tenant, app)


@pytest.mark.parametrize("value", ["!", "x" * 2049, "e30=", "bnVsbA=="])
def test_malformed_cursor_is_refused(value):
    with pytest.raises(ContractError, match="cursor"):
        position(value, "tenant", "app")


@pytest.mark.parametrize("tail", [["2026-11-20T00:00:00", "event"],
                                  ["invalid", "event"], ["2026-11-20T00:00:00Z", 4]])
def test_untyped_or_naive_cursor_is_refused(tail):
    value = base64.urlsafe_b64encode(json.dumps(["tenant", "app", *tail]).encode()).decode()
    with pytest.raises(ContractError, match="cursor"):
        position(value, "tenant", "app")
