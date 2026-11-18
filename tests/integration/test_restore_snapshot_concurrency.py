"""Deterministic real DB commits during COPY and independent corruption checks."""

from contextlib import contextmanager
import json
import os
from pathlib import Path

from handoff_drive import pieces
from postgres_business import TABLES, Route
from psycopg import sql
import pytest

from sova.domain.contracts import ContractError

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


def prepared(b):
    with b['connect'](b['names']['src']) as source, b['connect'](b['names']['tgt']) as target:
        for table, key in TABLES:
            with source.cursor().copy(sql.SQL('COPY {} TO STDOUT').format(sql.Identifier(table))) as out, \
                    target.cursor().copy(sql.SQL('COPY {} FROM STDIN').format(sql.Identifier(table))) as into:
                for chunk in out:
                    into.write(chunk)
    return pieces(b, 'postgres-offline-copy', source='src', target='tgt', route=Route(b['names']['src']))[1]


def counts(b, database):
    with b['connect'](database) as conn:
        return {table: conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
                for table, _ in TABLES}


def test_concurrent_commit_between_table_copies_is_not_misclassified_as_restore_corruption(business):
    b = business
    probes, injected = prepared(b), []
    connect = b['connect']
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        @contextmanager
        def copy(self, statement):
            with self.cursor.copy(statement) as stream:
                yield stream
            if not injected:
                with connect(b['names']['tgt']) as writer:
                    writer.execute("INSERT INTO orders VALUES (999,'concurrent','new-key',1)")
                    writer.execute("INSERT INTO work_items VALUES (999,999,'done')")
                    writer.execute("INSERT INTO idempotency_results VALUES ('concurrent','new-key','confirmed')")
                injected.append(True)
    class Source:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def __enter__(self):
            self.conn.__enter__()
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
        def cursor(self):
            return Cursor(self.conn.cursor())
    probes.admin_connect = lambda db: Source(connect(db)) if db == b['names']['tgt'] else connect(db)
    passed, observation = probes.restore_drill(b['names']['scratch'])
    current, restored = counts(b, b['names']['tgt']), counts(b, b['names']['scratch'])
    if directory := os.environ.get('SOVA_TEST_QUALIFICATION_ARTIFACTS'):
        (Path(directory) / 'restore-concurrent-commit.json').write_text(json.dumps({
            'commit_between_table_copies': bool(injected), 'probe_passed': passed, 'observation': observation,
            'current_target_counts': current, 'restored_snapshot_counts': restored,
            'qualification_issued': False, 'authorizes_execution': False}, indent=2) + '\n')
    assert injected and passed
    assert set(current.values()) == {51} and set(restored.values()) == {50}
    assert observation['source_isolation'] == 'repeatable read' and observation['source_read_only'] is True


def test_actual_restored_row_corruption_is_still_rejected(business):
    b, probes = business, prepared(business)
    with b['connect'](b['names']['scratch']) as conn:
        conn.execute("CREATE FUNCTION corrupt_restore() RETURNS trigger LANGUAGE plpgsql AS $$ "
                     "BEGIN NEW.amount := NEW.amount + 1; RETURN NEW; END $$")
        conn.execute("CREATE TRIGGER corrupt_restore BEFORE INSERT ON orders FOR EACH ROW EXECUTE FUNCTION corrupt_restore()")
    passed, observation = probes.restore_drill(b['names']['scratch'])
    assert not passed and observation['tables']['orders'] is False
    assert counts(b, b['names']['tgt'])['orders'] == 50


@pytest.mark.parametrize('which', ['src', 'tgt'])
def test_authoritative_database_cannot_be_used_as_destructive_scratch(business, which):
    b, probes = business, prepared(business)
    before = counts(b, b['names'][which])
    with pytest.raises(ContractError, match='scratch'):
        probes.restore_drill(b['names'][which])
    assert counts(b, b['names'][which]) == before
