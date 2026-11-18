"""Real PostgreSQL status reads; synthetic admission, no resource mutation."""
from datetime import datetime
import json

import pytest

from handoff_support import admitted_handoff
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.domain.operations import OperationRequest
from sova.service.recovery_status import read_status

pytestmark = pytest.mark.postgres


def prepare(postgres_database, now):
    h = admitted_handoff(postgres_database, now)
    h["unit"] = json.loads(h["revision"].canonical)["unit"]["unit_digest"]
    return h


def status(h):
    return read_status(h["store"], h["lifecycle"], h["app"], h["unit"])


def submit(h, operation="operation-1"):
    PostgresOperationRepository(h["lifecycle"]).submit(OperationRequest(
        h["tenant"], h["app"], operation, h["plan"].plan_id, h["plan"].fingerprint,
        "approval-1", 1, "test-operator", operation+"-key"))


def test_status_does_not_invent_authority_or_treat_open_without_operation_as_clear(postgres_database, now):
    h = prepare(postgres_database, now)
    assert status(h) == {"state": None}
    original = h["coordinator"].open(h["revision"], h["plan"], actor_id="operator")
    view = status(h)
    assert view["state"] == "SOURCE_ACTIVE"
    assert view["scope"]["plan_digest"] == h["plan"].fingerprint
    assert view["scope"]["operation_id"] is None
    assert view["recovery"]["status"] == "unavailable"
    assert view["recovery"]["required"] is None
    assert h["store"].current(h["app"], h["unit"]) == original
    assert len(h["store"].history(h["app"], h["unit"])) == 1


def test_status_is_plan_bound_and_never_returns_execution_token(postgres_database, now):
    h = prepare(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="operator")
    submit(h)
    view = status(h)
    assert view["scope"]["operation_id"] == "operation-1"
    assert view["recovery"]["status"] == "clear"
    assert view["recovery"]["authorizes_execution"] is False
    assert view["recovery"]["automatic_replay_allowed"] is False
    assert (datetime.fromisoformat(view["expires_at"])-datetime.fromisoformat(view["observed_at"])).total_seconds() == 5
    assert not {"token", "command_document", "parameters", "connection_string"} & set(json.dumps(view).split('"'))
    with h["lifecycle"].transaction() as conn:
        conn.execute("UPDATE sova_control.operations SET security_hold=true "
                     "WHERE tenant_id=%s AND application_id=%s", (h["tenant"], h["app"]))
    held = status(h)
    assert held["state"] == "SOURCE_ACTIVE"
    assert held["recovery"]["reason"] == "SECURITY_HOLD"


def test_ambiguous_operations_do_not_hide_behind_a_clear_status(postgres_database, now):
    h = prepare(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="operator")
    submit(h)
    submit(h, "second-operation")
    view = status(h)
    assert view["recovery"]["status"] == "unavailable"
    assert view["recovery"]["required"] is None
    assert view["scope"]["operation_id"] is None
