"""Registry documents with revisions, written by compare-and-set (§13.1).

``register`` accepts revision ``n + 1`` of a subject only from a writer that
read revision ``n`` (``expected_revision``; 0 for a new subject). Two writers
that read the same revision cannot both win: the head row moves in one
conditional statement, and the loser gets ``RegistrationConflict`` naming the
revision that won. A retry of a write that already succeeded (same document,
response lost) returns that revision instead of a conflict.

Every accepted revision stays in the history, unchanged: the runtime role may
insert but never update or delete it, and loading recomputes the digest and
re-decodes the document. Rows are tenant-scoped by row security.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json

from sova.domain.contracts import ContractError, canonical_json, identifier, utc
from sova.adapters.registration import registry_codec as codecs


class RegistrationConflict(ContractError):
    def __init__(self, current_revision):
        super().__init__(f"Registration changed: current revision is {current_revision}")
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class Registration:
    kind: str
    subject_id: str
    revision: int
    digest: str
    document: dict
    registered_by: str
    registered_at: datetime

    @property
    def value(self):
        return codecs.decode(self.kind, self.document)


class PostgresRegistrations:
    def __init__(self, connect, *, tenant_id):
        self.connect = connect
        self.tenant_id = identifier(tenant_id, "tenant_id")

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            with conn.transaction():
                privileged = conn.execute("""SELECT rolsuper OR rolbypassrls OR EXISTS (
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='sova_control' AND c.relowner=pg_roles.oid)
                    FROM pg_roles WHERE rolname=current_user""").fetchone()
                if not privileged or privileged[0]:
                    raise ContractError(
                        "Registry runtime role must not own or bypass row security")
                conn.execute("SELECT set_config('sova.tenant_id',%s,true)", (self.tenant_id,))
                yield conn

    def _checked(self, kind, subject_id, document):
        value = codecs.decode(kind, document)
        if codecs.subject_of(value) != subject_id:
            raise ContractError("The document names another subject")
        tenant = codecs.tenant_of(value)
        if tenant is not None and tenant != self.tenant_id:
            raise ContractError("The document belongs to another tenant")
        return value

    def register(self, kind, subject_id, document, *, expected_revision, registered_by):
        identifier(registered_by, "registered_by")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ContractError("Expected revision required (0 for a new subject)")
        value = self._checked(kind, subject_id, document)
        digest, canonical = value.fingerprint, canonical_json(document, limit=1048576)
        revision = expected_revision + 1
        with self.transaction() as conn:
            if expected_revision == 0:
                moved = conn.execute(
                    """INSERT INTO sova_control.registration_heads
                    (tenant_id,kind,subject_id,revision,digest) VALUES(%s,%s,%s,1,%s)
                    ON CONFLICT DO NOTHING RETURNING 1""",
                    (self.tenant_id, kind, subject_id, digest)).fetchone()
            else:
                moved = conn.execute(
                    """UPDATE sova_control.registration_heads
                    SET revision=%s, digest=%s, updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND kind=%s AND subject_id=%s AND revision=%s
                    RETURNING 1""",
                    (revision, digest, self.tenant_id, kind, subject_id,
                     expected_revision)).fetchone()
            if moved:
                conn.execute(
                    """INSERT INTO sova_control.registrations
                    (tenant_id,kind,subject_id,revision,digest,canonical,registered_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (self.tenant_id, kind, subject_id, revision, digest, canonical,
                     registered_by))
        if moved:
            return self.at(kind, subject_id, revision)
        current = self.current(kind, subject_id)
        if current is not None and current.revision == revision and current.digest == digest:
            return current  # the same write, retried after a lost response
        raise RegistrationConflict(current.revision if current else 0)

    def _load(self, row, kind, subject_id):
        revision, digest, canonical, registered_by, registered_at = row
        document = json.loads(canonical)
        value = self._checked(kind, subject_id, document)
        if value.fingerprint != digest:
            raise ContractError("Stored registration differs from its digest")
        return Registration(kind, subject_id, revision, digest, document, registered_by,
                            utc(registered_at))

    def at(self, kind, subject_id, revision):
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT revision,digest,canonical,registered_by,registered_at
                FROM sova_control.registrations
                WHERE tenant_id=%s AND kind=%s AND subject_id=%s AND revision=%s""",
                (self.tenant_id, kind, subject_id, revision)).fetchone()
        if row is None:
            raise ContractError("Unknown registration revision")
        return self._load(row, kind, subject_id)

    def current(self, kind, subject_id):
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT r.revision,r.digest,r.canonical,r.registered_by,r.registered_at
                FROM sova_control.registration_heads h JOIN sova_control.registrations r
                USING (tenant_id,kind,subject_id,revision)
                WHERE h.tenant_id=%s AND h.kind=%s AND h.subject_id=%s""",
                (self.tenant_id, kind, subject_id)).fetchone()
        return None if row is None else self._load(row, kind, subject_id)

    def history(self, kind, subject_id):
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT revision,digest,registered_by,registered_at
                FROM sova_control.registrations
                WHERE tenant_id=%s AND kind=%s AND subject_id=%s ORDER BY revision""",
                (self.tenant_id, kind, subject_id)).fetchall()
        return [{"revision": r, "digest": d, "registered_by": b, "registered_at": utc(t)}
                for r, d, b, t in rows]

    def subjects(self, kind):
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT subject_id,revision,digest,updated_at
                FROM sova_control.registration_heads
                WHERE tenant_id=%s AND kind=%s ORDER BY subject_id""",
                (self.tenant_id, kind)).fetchall()
        return [{"subject_id": s, "revision": r, "digest": d, "updated_at": utc(t)}
                for s, r, d, t in rows]
