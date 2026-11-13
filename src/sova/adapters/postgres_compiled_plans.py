"""Content-addressed store of frozen compiled plans (MIG-SPEC-001 §13.3).

A plan is stored under the SHA-256 of its canonical text and is never
changed. Loading recomputes the digest and checks the tenant inside the
document, so a modified row is refused rather than executed. Same runtime-role
rules as the lifecycle repository: non-owner, NOBYPASSRLS, tenant set by
trusted callers.
"""

from contextlib import contextmanager
import json

from sova.domain.contracts import ContractError, digest, identifier
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan
from sova.ports.plans import RepositoryConflict


class PostgresCompiledPlans:
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
                        "Compiled plan runtime role must not own or bypass row security"
                    )
                conn.execute(
                    "SELECT set_config('sova.tenant_id',%s,true)", (self.tenant_id,)
                )
                yield conn

    def _check(self, revision):
        if not isinstance(revision, PlanRevision):
            raise ContractError("Frozen compiled plan required")
        doc = json.loads(revision.canonical)
        if doc.get("tenant_id") != self.tenant_id:
            raise ContractError("Compiled plan belongs to another tenant")
        return doc

    def save(self, revision):
        """Store once; storing the same digest again is a no-op."""
        doc = self._check(revision)
        with self.transaction() as conn:
            inserted = conn.execute(
                """INSERT INTO sova_control.compiled_plans
                (tenant_id,application_id,compiled_digest,canonical) VALUES(%s,%s,%s,%s)
                ON CONFLICT (tenant_id,compiled_digest) DO NOTHING RETURNING 1""",
                (self.tenant_id, doc["service_id"], revision.digest, revision.canonical),
            ).fetchone()
        if not inserted and self.load(revision.digest) != revision:
            raise RepositoryConflict("Stored compiled plan differs from its digest")
        return revision.digest

    def load(self, compiled_digest):
        digest(compiled_digest)
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT canonical FROM sova_control.compiled_plans
                WHERE tenant_id=%s AND compiled_digest=%s""",
                (self.tenant_id, compiled_digest),
            ).fetchone()
        if row is None:
            raise ContractError("Compiled plan not found")
        revision = PlanRevision(row[0])
        if revision.digest != compiled_digest:
            raise ContractError("Stored compiled plan does not match its digest")
        self._check(revision)
        return revision

    def for_plan(self, plan):
        """The frozen plan an approved lifecycle plan was lowered from."""
        if not isinstance(plan, LifecyclePlan) or plan.origin_plan_digest is None:
            raise ContractError("Lowered handoff plan required")
        return self.load(plan.origin_plan_digest)
