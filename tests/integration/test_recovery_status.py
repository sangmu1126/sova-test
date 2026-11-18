"""Real PostgreSQL status reads; synthetic admission, no resource mutation."""
from datetime import datetime
import json
import psycopg

import pytest
from psycopg.types.json import Jsonb

from handoff_support import admitted_handoff
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.domain.operations import OperationRequest
from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.domain.contracts import ContractError, ResourceScope
from sova.security.domain.governance import SecurityHold, StoredHold
from sova.security.adapters.documents import document_hash
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
    assert view["recovery"]["security_holds"]["operation_hold"] is None
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
    # Seed the fixture-only operation flag as its migration owner. The normal
    # runtime intentionally lacks direct UPDATE on this protected column.
    with psycopg.connect(postgres_database[0]) as conn:
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


@pytest.mark.parametrize("corruption", ["digest", "scope"])
def test_corrupt_governance_hold_cannot_be_displayed_as_clear(postgres_database, now, corruption):
    h = prepare(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="operator")
    submit(h)
    holds = PostgresSecurityHolds(h["connect"], tenant_id=h["tenant"])
    scope = ResourceScope(h["tenant"],h["app"],"source","database")
    holds.open_hold(SecurityHold("hold-corrupt",scope,"raiser","private detail",now))
    with psycopg.connect(postgres_database[0]) as conn:
        if corruption == "digest":
            conn.execute("UPDATE sova_control.security_hold_events SET digest=%s "
                "WHERE tenant_id=%s AND application_id=%s AND hold_id='hold-corrupt'",
                ("0"*64,h["tenant"],h["app"]))
        else:
            row = conn.execute("SELECT document FROM sova_control.security_hold_events "
                "WHERE tenant_id=%s AND application_id=%s AND hold_id='hold-corrupt'",
                (h["tenant"],h["app"])).fetchone()[0]
            row["hold"]["scope"]["tenant_id"] = "other-tenant"
            conn.execute("UPDATE sova_control.security_hold_events SET document=%s,digest=%s "
                "WHERE tenant_id=%s AND application_id=%s AND hold_id='hold-corrupt'",
                (Jsonb(row),document_hash(row),h["tenant"],h["app"]))
    with pytest.raises(ContractError):
        status(h)


def test_governance_limit_does_not_silently_drop_the_last_hold(postgres_database, now):
    h = prepare(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="operator")
    submit(h)
    scope = ResourceScope(h["tenant"],h["app"],"source","database")
    # Bulk synthetic incident rows in this disposable fixture; use the same
    # domain document/hash as the existing writer. Product grants are unchanged.
    with psycopg.connect(postgres_database[0]) as conn:
        with conn.cursor() as cursor:
            rows=[]
            for i in range(257):
                hold=StoredHold(SecurityHold(f"hold-{i:03}",scope,"raiser","test",now),1)
                rows.append((h["tenant"],h["app"],hold.hold.hold_id,document_hash(hold.document),Jsonb(hold.document)))
            cursor.executemany("INSERT INTO sova_control.security_hold_events "
                "(tenant_id,application_id,hold_id,revision,event_kind,digest,document) "
                "VALUES(%s,%s,%s,1,'opened',%s,%s)",rows)
    with pytest.raises(ContractError,match="limit"):
        status(h)
