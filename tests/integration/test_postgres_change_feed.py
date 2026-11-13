"""Change events and collector watermarks in real PostgreSQL (INV-08).

Synthetic tenants and events only; no live environment is observed or changed.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from contract_world import build_world
from handoff_support import admitted_handoff
import psycopg
import pytest

from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.application.plan_reaffirmation import PlanReaffirmation
from sova.domain.change_impact import ChangeEvent, ChangeKind, PlanReevaluationRequired
from sova.domain.contracts import ContractError
from sova.ports.plans import RepositoryConflict

pytestmark = pytest.mark.postgres


@pytest.fixture
def feeds(postgres_database):
    dsn, role = postgres_database

    def connect():
        return psycopg.connect(dsn, user=role)

    tenants = ("tenant-" + uuid4().hex, "tenant-" + uuid4().hex)
    return tenants, [PostgresChangeFeed(connect, tenant_id=t) for t in tenants]


def change(tenant, event_id, *, kind=ChangeKind.RESOURCE, subject="uid-1", at=None,
           cause=None):
    return ChangeEvent(event_id, tenant, "orders-service", kind, "source-env", subject, None,
                       at or datetime.now(UTC) - timedelta(seconds=1), cause)


def test_events_are_append_only_and_idempotent_by_id(feeds):
    (tenant, _), (feed, _) = feeds
    first = change(tenant, "ev-1", cause=("a" * 64, "step-1"))
    assert feed.record(first, collector_id="collector-a") is True
    assert feed.record(first, collector_id="collector-a") is False  # safe retry
    with pytest.raises(RepositoryConflict, match="different content"):
        feed.record(change(tenant, "ev-1", subject="uid-2"), collector_id="collector-a")
    read = feed.events(application_id="orders-service", environments=("source-env",),
                       since=first.observed_at - timedelta(seconds=1))
    assert read == (first,)
    assert feed.events(application_id="other-service", environments=("other-env",),
                       since=first.observed_at - timedelta(seconds=1)) == ()


def test_watermarks_belong_to_one_collector_and_never_move_backwards(feeds):
    _, (feed, _) = feeds
    now = datetime.now(UTC)
    feed.advance("source-env", collector_id="collector-a",
                 observed_through=now - timedelta(seconds=5))
    mark = feed.advance("source-env", collector_id="collector-a",
                        observed_through=now - timedelta(seconds=1))
    assert feed.watermarks(("source-env",)) == (mark,)
    with pytest.raises(RepositoryConflict, match="backwards"):
        feed.advance("source-env", collector_id="collector-a",
                     observed_through=now - timedelta(seconds=3))
    with pytest.raises(RepositoryConflict, match="another collector"):
        feed.advance("source-env", collector_id="collector-b", observed_through=now)
    with pytest.raises(ContractError, match="ahead of the database clock"):
        feed.advance("source-env", collector_id="collector-a",
                     observed_through=now + timedelta(minutes=5))


def test_tenants_never_see_each_others_changes(feeds):
    (tenant, _), (feed, other) = feeds
    feed.record(change(tenant, "ev-private"), collector_id="collector-a")
    feed.advance("source-env", collector_id="collector-a",
                 observed_through=datetime.now(UTC) - timedelta(seconds=1))
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert other.events(application_id="orders-service", environments=("source-env",),
                        since=since) == ()
    assert other.watermarks(("source-env",)) == ()
    with pytest.raises(ContractError, match="this tenant"):
        other.record(change(tenant, "ev-cross"), collector_id="collector-a")


def test_reaffirmations_are_tenant_scoped_and_not_ahead_of_the_clock(feeds):
    (tenant, _), (feed, other) = feeds
    now = datetime.now(UTC)
    for seconds in (5, 2):
        feed.record_reaffirmation(
            application_id="orders-service", plan_digest="a" * 64, compiled_digest="b" * 64,
            snapshot_digest=f"{seconds:x}" * 64, observed_at=now - timedelta(seconds=seconds),
            actor_id="planner")
    assert feed.reaffirmed_at("a" * 64) == now - timedelta(seconds=2)
    assert other.reaffirmed_at("a" * 64) is None
    with pytest.raises(ContractError, match="ahead of the database clock"):
        feed.record_reaffirmation(
            application_id="orders-service", plan_digest="a" * 64, compiled_digest="b" * 64,
            snapshot_digest="c" * 64, observed_at=now + timedelta(minutes=5),
            actor_id="planner")


def test_reaffirmed_plan_issues_commands_again(postgres_database, now):
    h = admitted_handoff(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    h["feed"].record(ChangeEvent(
        "ev-db-config", h["tenant"], "another-service", ChangeKind.RESOURCE,
        "synthetic-source", "synthetic-uid-database", None,
        datetime.now(UTC) - timedelta(milliseconds=20)), collector_id="change-collector-1")
    with pytest.raises(PlanReevaluationRequired):
        h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op", epoch=1)
    at = datetime.now(UTC) - timedelta(milliseconds=5)
    world = build_world(at, contract=h["world"]["contract"])
    world["sign"](world["contract"])
    world["sign"](world["snapshot"])
    result = PlanReaffirmation(feed=h["feed"], clock=lambda: datetime.now(UTC)).reaffirm(
        h["revision"], h["plan"], world["contract"], world["snapshot"],
        expected_tenant=h["tenant"], authenticate=world["auth"], actor_id="planner")
    assert result["status"] == "REAFFIRMED"
    command = h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op",
                                            epoch=1)
    assert command.action == "stage_target"


def test_registry_history_is_read_regardless_of_the_snapshot_time(feeds):
    (tenant, _), (feed, _) = feeds
    old = datetime.now(UTC) - timedelta(hours=2)
    feed.record(ChangeEvent("ev-contract-r8", tenant, "orders-service", ChangeKind.CONTRACT,
                            None, None, "f" * 64, old), collector_id="registry-collector")
    feed.record(change(tenant, "ev-old-resource", at=old), collector_id="collector-a")
    read = feed.events(application_id="orders-service", environments=("source-env",),
                       since=datetime.now(UTC) - timedelta(minutes=1))
    assert [e.event_id for e in read] == ["ev-contract-r8"]
