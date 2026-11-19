"""Snapshot differences invalidate evidence; equal snapshots prove no history."""
from contextlib import contextmanager
from datetime import UTC, datetime

from sova.adapters.postgres_change_collector import PostgresCatalogCollector
from sova.domain.change_impact import ChangeKind


class Feed:
    def __init__(self):
        self.records = []

    def record(self, event, *, collector_id):
        self.records.append(event)

    def advance(self, *args, **kwargs):
        raise AssertionError('Snapshot polling cannot certify exhaustive change coverage')


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class Catalog:
    def __init__(self):
        self.columns = [('orders', 'id', 'bigint', 'NO')]
        self.acl = [('writer', 'orders', 'INSERT')]

    @contextmanager
    def connect(self, database):
        yield self

    def execute(self, statement, args=()):
        if statement.startswith('SELECT clock_timestamp'):
            return Result([(datetime.now(UTC),)])
        if 'information_schema.columns' in statement:
            return Result(list(self.columns))
        if 'role_table_grants' in statement:
            return Result(list(self.acl))
        return Result([('fixed-database-acl',)])


def test_actual_differences_remain_invalidations_without_advancing_a_watermark():
    db, feed = Catalog(), Feed()
    c = PostgresCatalogCollector(feed, connect=db.connect, tenant_id='tenant-a', application_id='orders', collector_id='catalog-1', databases={'source': ('env-source', 'db-uid')})
    assert c.poll() == ()
    db.acl.append(('other', 'orders', 'INSERT'))
    assert [e.kind for e in c.poll()] == [ChangeKind.POLICY]
    assert len(feed.records) == 1


def test_change_restored_between_polls_never_becomes_false_coverage():
    db, feed = Catalog(), Feed()
    c = PostgresCatalogCollector(feed, connect=db.connect, tenant_id='tenant-a', application_id='orders', collector_id='catalog-1', databases={'source': ('env-source', 'db-uid')})
    assert c.poll() == ()
    original = list(db.columns)
    db.columns.append(('orders', 'transient', 'text', 'YES'))
    db.columns = original
    assert c.poll() == ()  # a snapshot cannot detect the intervening history
    assert not feed.records  # and, critically, Feed.advance was never called
