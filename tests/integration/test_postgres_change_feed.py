"""Change events and collector watermarks in real PostgreSQL (INV-08).

Synthetic tenants and events only; no live environment is observed or changed.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.domain.change_impact import ChangeEvent, ChangeKind
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
