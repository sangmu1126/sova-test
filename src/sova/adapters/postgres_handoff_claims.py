"""Atomic cross-plan handoff claims in PostgreSQL (MIG-SPEC-001 §12.5).

One transaction locks the approval row against revocation, takes
transaction-scoped advisory locks in the fixed claim order, checks active
claims, and records the new claims. Waiting on a key's advisory lock makes a
concurrent claimant's committed rows visible to the next statement, so two
conflicting plans cannot both be admitted. Same runtime-role rules as the
lifecycle repository: non-owner, NOBYPASSRLS, tenant set by trusted callers.
"""

from contextlib import contextmanager
import json

from sova.domain.contracts import ContractError, digest, identifier
from sova.ports.plans import RepositoryConflict

_MODES = ("read", "write")


def require_no_security_hold(conn, connect, tenant_id, application_id):
    """Serialize with hold changes and refuse while any hold is active (Part IV)."""
    from sova.adapters.postgres_governance import (
        PostgresSecurityHolds,
        lock_security_hold_set,
    )

    lock_security_hold_set(conn, tenant_id, application_id)
    if PostgresSecurityHolds(connect, tenant_id=tenant_id)._active(conn, application_id):
        raise RepositoryConflict("Active security hold blocks handoff progress")


def _key_text(key):
    if type(key) is not tuple or not key or any(type(p) is not str for p in key):
        raise ContractError("Absolute claim key required")
    return json.dumps(list(key), separators=(",", ":"), ensure_ascii=False)


class PostgresHandoffClaims:
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
                        "Claim runtime role must not own or bypass row security"
                    )
                conn.execute(
                    "SELECT set_config('sova.tenant_id',%s,true)", (self.tenant_id,)
                )
                yield conn

    def acquire(
        self,
        *,
        application_id,
        plan_id,
        plan_digest,
        compiled_digest,
        approval_id,
        approval_revision,
        claims,
    ):
        for value in (application_id, plan_id, approval_id):
            identifier(value)
        digest(plan_digest)
        digest(compiled_digest)
        if type(approval_revision) is not int or approval_revision < 1:
            raise ContractError("Positive approval revision required")
        if type(claims) is not tuple or not 1 <= len(claims) <= 512:
            raise ContractError("Bounded claim set required")
        encoded = {}
        for key, mode in claims:
            if mode not in _MODES:
                raise ContractError("Typed claim mode required")
            text = _key_text(key)
            if encoded.get(text) != "write":
                encoded[text] = mode
        ordered = sorted(encoded.items())
        with self.transaction() as conn:
            approval = conn.execute(
                """SELECT plan_id,plan_digest,revision,revoked_at FROM sova_control.approvals
                WHERE tenant_id=%s AND application_id=%s AND approval_id=%s FOR SHARE""",
                (self.tenant_id, application_id, approval_id),
            ).fetchone()
            if (
                not approval
                or approval[0] != plan_id
                or approval[1] != plan_digest
                or approval[2] != approval_revision
                or approval[3] is not None
            ):
                raise RepositoryConflict("Approval changed, revoked, or not for this plan")
            require_no_security_hold(conn, self.connect, self.tenant_id, application_id)
            conflicts = []
            for text, mode in ordered:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("sova.handoff:" + text,),
                )
                rows = conn.execute(
                    """SELECT plan_digest,mode FROM sova_control.handoff_claims
                    WHERE tenant_id=%s AND claim_key=%s AND released_at IS NULL""",
                    (self.tenant_id, text),
                ).fetchall()
                for other_digest, other_mode in rows:
                    if other_digest == plan_digest:
                        raise RepositoryConflict("Plan was already admitted")
                    if "write" in (mode, other_mode):
                        conflicts.append((text, other_digest))
            if conflicts:
                raise RepositoryConflict(
                    "Overlapping active handoff claims: "
                    + ", ".join(sorted({d[:12] for _, d in conflicts}))
                )
            released = conn.execute(
                """SELECT 1 FROM sova_control.handoff_claims
                WHERE tenant_id=%s AND plan_digest=%s LIMIT 1""",
                (self.tenant_id, plan_digest),
            ).fetchone()
            if released:
                raise RepositoryConflict("A plan is admitted at most once")
            for text, mode in ordered:
                conn.execute(
                    """INSERT INTO sova_control.handoff_claims
                    (tenant_id,application_id,plan_id,plan_digest,compiled_digest,
                     approval_id,claim_key,mode) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        self.tenant_id,
                        application_id,
                        plan_id,
                        plan_digest,
                        compiled_digest,
                        approval_id,
                        text,
                        mode,
                    ),
                )
            conn.execute(
                """INSERT INTO sova_control.lifecycle_audit
                (tenant_id,application_id,record_id,action,actor_id,revision)
                VALUES(%s,%s,%s,'handoff_claimed',%s,%s)""",
                (self.tenant_id, application_id, plan_id, approval_id, approval_revision),
            )
            return len(ordered)

    def release(self, *, application_id, plan_digest, actor_id):
        """End claims after the handoff completes or is safely abandoned.

        Releasing claims is not a recovery decision; callers must already have
        reached a terminal, verified handoff state.
        """
        identifier(application_id)
        identifier(actor_id, "actor_id")
        digest(plan_digest)
        with self.transaction() as conn:
            rows = conn.execute(
                """UPDATE sova_control.handoff_claims
                SET released_at=clock_timestamp(), released_by=%s
                WHERE tenant_id=%s AND application_id=%s AND plan_digest=%s
                  AND released_at IS NULL RETURNING plan_id""",
                (actor_id, self.tenant_id, application_id, plan_digest),
            ).fetchall()
            if not rows:
                raise RepositoryConflict("No active claims for this plan")
            conn.execute(
                """INSERT INTO sova_control.lifecycle_audit
                (tenant_id,application_id,record_id,action,actor_id,revision)
                VALUES(%s,%s,%s,'handoff_released',%s,1)""",
                (self.tenant_id, application_id, rows[0][0], actor_id),
            )
            return len(rows)

    def active(self, *, application_id=None):
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT application_id,plan_digest,claim_key,mode
                FROM sova_control.handoff_claims
                WHERE tenant_id=%s AND released_at IS NULL
                  AND (%s::text IS NULL OR application_id=%s)
                ORDER BY claim_key,plan_digest""",
                (self.tenant_id, application_id, application_id),
            ).fetchall()
        return tuple(
            (app, plan, tuple(json.loads(key)), mode) for app, plan, key, mode in rows
        )
