"""Per-unit authority records with compare-and-set and an append-only history.

Only an admitted plan (active claims) may open or change a unit's record, and
a unit can start a new plan only after its previous record is terminal. Every
change is a compare-and-set on ``revision`` in the same transaction as its
history row, so a stale coordinator cannot overwrite a newer decision.
"""

from contextlib import contextmanager

from psycopg.types.json import Jsonb

from sova.domain.contracts import ContractError, digest, identifier
from sova.adapters.postgres_handoff_claims import require_no_security_hold
from sova.domain.handoff_authority import (
    TERMINAL,
    AuthorityRecord,
    HandoffState,
    record_from_document,
)
from sova.ports.plans import RepositoryConflict

# Under an active security hold only moves toward safety are recorded.
_SAFETY_WARD = {HandoffState.SAFE_HOLD, HandoffState.FAILBACK_REQUIRED}


class PostgresHandoffAuthority:
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
                        "Authority runtime role must not own or bypass row security"
                    )
                conn.execute(
                    "SELECT set_config('sova.tenant_id',%s,true)", (self.tenant_id,)
                )
                yield conn

    def _current(self, conn, application_id, unit_digest, *, lock):
        row = conn.execute(
            """SELECT plan_digest,compiled_digest,revision,state,document
            FROM sova_control.handoff_authority
            WHERE tenant_id=%s AND application_id=%s AND unit_digest=%s"""
            + (" FOR UPDATE" if lock else ""),
            (self.tenant_id, application_id, unit_digest),
        ).fetchone()
        if not row:
            return None
        record = record_from_document(row[4])
        if (
            record.tenant_id,
            record.application_id,
            record.unit_digest,
            record.plan_digest,
            record.compiled_digest,
            record.revision,
            str(record.state),
        ) != (self.tenant_id, application_id, unit_digest, *row[:4]):
            raise ContractError("Stored authority record integrity mismatch")
        return record

    def current(self, application_id, unit_digest):
        identifier(application_id)
        digest(unit_digest)
        with self.transaction() as conn:
            return self._current(conn, application_id, unit_digest, lock=False)

    def _require_admitted(self, conn, record):
        admitted = conn.execute(
            """SELECT 1 FROM sova_control.handoff_claims
            WHERE tenant_id=%s AND application_id=%s AND plan_digest=%s
              AND compiled_digest=%s AND released_at IS NULL LIMIT 1""",
            (self.tenant_id, record.application_id, record.plan_digest,
             record.compiled_digest),
        ).fetchone()
        if not admitted:
            raise RepositoryConflict("Plan is not admitted or its claims were released")

    def _event(self, conn, record, action, actor_id):
        conn.execute(
            """INSERT INTO sova_control.handoff_authority_events
            (tenant_id,application_id,unit_digest,plan_digest,revision,action,actor_id,document)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (self.tenant_id, record.application_id, record.unit_digest,
             record.plan_digest, record.revision, action, actor_id,
             Jsonb(record.document)),
        )

    def _require_failback_allowed(self, conn, record):
        """A failback continues a forward handoff in FAILBACK_REQUIRED: its epoch
        is higher, and no other failback of it is open or has taken authority."""
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("sova.failback:" + record.predecessor,))
        forward = conn.execute(
            """SELECT document FROM sova_control.handoff_authority
            WHERE tenant_id=%s AND application_id=%s AND compiled_digest=%s FOR UPDATE""",
            (self.tenant_id, record.application_id, record.predecessor),
        ).fetchone()
        if not forward:
            raise RepositoryConflict("Forward handoff authority not found")
        forward = record_from_document(forward[0])
        if forward.state != HandoffState.FAILBACK_REQUIRED or record.epoch <= forward.epoch:
            raise RepositoryConflict("Forward handoff does not require this failback")
        others = conn.execute(
            """SELECT state FROM sova_control.handoff_authority
            WHERE tenant_id=%s AND application_id=%s AND document->>'predecessor'=%s""",
            (self.tenant_id, record.application_id, record.predecessor),
        ).fetchall()
        if any(state != str(HandoffState.ABORTED) for (state,) in others):
            raise RepositoryConflict("Another failback of this handoff is open or done")

    def open(self, record, *, actor_id):
        if not isinstance(record, AuthorityRecord) or record.revision != 1:
            raise ContractError("A new authority record starts at revision one")
        if record.tenant_id != self.tenant_id:
            raise ContractError("Authority tenant scope mismatch")
        identifier(actor_id, "actor_id")
        with self.transaction() as conn:
            self._require_admitted(conn, record)
            require_no_security_hold(conn, self.connect, self.tenant_id,
                                     record.application_id)
            if record.predecessor is not None:
                self._require_failback_allowed(conn, record)
            previous = self._current(
                conn, record.application_id, record.unit_digest, lock=True
            )
            values = (record.plan_digest, record.compiled_digest, record.revision,
                      str(record.state), Jsonb(record.document))
            if previous is None:
                conn.execute(
                    """INSERT INTO sova_control.handoff_authority
                    (tenant_id,application_id,unit_digest,plan_digest,compiled_digest,
                     revision,state,document) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (self.tenant_id, record.application_id, record.unit_digest, *values),
                )
            elif previous.state not in TERMINAL:
                raise RepositoryConflict("Unit already has an unfinished handoff")
            elif previous.plan_digest == record.plan_digest:
                raise RepositoryConflict("A finished plan cannot be reopened")
            else:
                changed = conn.execute(
                    """UPDATE sova_control.handoff_authority
                    SET plan_digest=%s,compiled_digest=%s,revision=%s,state=%s,
                        document=%s,updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND application_id=%s AND unit_digest=%s
                      AND plan_digest=%s AND revision=%s RETURNING 1""",
                    (*values, self.tenant_id, record.application_id,
                     record.unit_digest, previous.plan_digest, previous.revision),
                ).fetchone()
                if not changed:
                    raise RepositoryConflict("Unit authority changed concurrently")
            self._event(conn, record, "opened", actor_id)
        return record

    def transition(self, before, after, *, action, actor_id):
        if not isinstance(before, AuthorityRecord) or not isinstance(after, AuthorityRecord):
            raise ContractError("Typed authority records required")
        if (
            after.revision != before.revision + 1
            or (after.tenant_id, after.application_id, after.unit_digest,
                after.plan_digest, after.compiled_digest)
            != (before.tenant_id, before.application_id, before.unit_digest,
                before.plan_digest, before.compiled_digest)
        ):
            raise ContractError("Authority transition must be the next revision of one plan")
        identifier(action, "action")
        identifier(actor_id, "actor_id")
        with self.transaction() as conn:
            self._require_admitted(conn, after)
            if after.state not in _SAFETY_WARD:
                require_no_security_hold(conn, self.connect, self.tenant_id,
                                         after.application_id)
            changed = conn.execute(
                """UPDATE sova_control.handoff_authority
                SET revision=%s,state=%s,document=%s,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND application_id=%s AND unit_digest=%s
                  AND plan_digest=%s AND revision=%s RETURNING 1""",
                (after.revision, str(after.state), Jsonb(after.document),
                 self.tenant_id, after.application_id, after.unit_digest,
                 after.plan_digest, before.revision),
            ).fetchone()
            if not changed:
                raise RepositoryConflict("Stale authority revision")
            self._event(conn, after, action, actor_id)
        return after

    def history(self, application_id, unit_digest):
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT revision,action,actor_id,document FROM sova_control.handoff_authority_events
                WHERE tenant_id=%s AND application_id=%s AND unit_digest=%s
                ORDER BY event_id""",
                (self.tenant_id, application_id, unit_digest),
            ).fetchall()
        return tuple((rev, action, actor, record_from_document(doc))
                     for rev, action, actor, doc in rows)

    def active_hold(self, application_id):
        """Whether a security hold is active now (read, serialized with hold changes)."""
        identifier(application_id)
        try:
            self.require_no_hold(application_id)
        except RepositoryConflict:
            return True
        return False

    def require_no_hold(self, application_id):
        """Checked before issuing a command; the effect ledger rechecks its own grant."""
        identifier(application_id)
        with self.transaction() as conn:
            require_no_security_hold(conn, self.connect, self.tenant_id, application_id)
