"""Bounded read models for a user-driven workspace; no alternate executor."""

import json

from sova.adapters.postgres_registrations import PostgresRegistrations
from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.registration import registry_codec as codecs
from sova.domain.contracts import ContractError, identifier

PAGE_SIZE = 20


def registrations(connect, tenant, kind, after=""):
    if kind not in codecs.KINDS:
        raise ContractError("Unknown registry kind")
    if after:
        identifier(after, "registry cursor")
    registry = PostgresRegistrations(connect, tenant_id=tenant)
    with registry.transaction() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        rows = conn.execute(
            """SELECT h.subject_id,h.digest,r.revision,r.digest,r.canonical,
                r.registered_by,r.registered_at
            FROM sova_control.registration_heads h LEFT JOIN sova_control.registrations r
                USING(tenant_id,kind,subject_id,revision)
            WHERE h.tenant_id=%s AND h.kind=%s AND h.subject_id>%s
            ORDER BY h.subject_id LIMIT %s""",
            (tenant, kind, after, PAGE_SIZE + 1),
        ).fetchall()
        values = []
        for row in rows[:PAGE_SIZE]:
            if row[2] is None:
                raise ContractError("Registry head names a missing revision")
            record = registry._load(row[2:], kind, row[0])
            if record.digest != row[1]:
                raise ContractError("Registry head differs from immutable revision")
            values.append(
                {
                    "kind": kind,
                    "subject_id": record.subject_id,
                    "revision": record.revision,
                    "digest": record.digest,
                    "registered_by": record.registered_by,
                    "registered_at": record.registered_at.isoformat(),
                }
            )
        return {
            "items": values,
            "next_after": values[-1]["subject_id"] if len(rows) > PAGE_SIZE else None,
        }


def plans(lifecycle, application, after=""):
    identifier(application, "application_id")
    if after:
        identifier(after, "plan cursor")
    with lifecycle.transaction() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        rows = conn.execute(
            """SELECT plan_id FROM sova_control.plans
            WHERE tenant_id=%s AND application_id=%s AND plan_id>%s
            ORDER BY plan_id LIMIT %s""",
            (lifecycle.tenant_id, application, after, PAGE_SIZE + 1),
        ).fetchall()
        values = []
        for (plan_id,) in rows[:PAGE_SIZE]:
            plan = lifecycle._get_plan(conn, application, plan_id)
            values.append(
                {
                    "plan_id": plan.plan_id,
                    "plan_digest": plan.fingerprint,
                    "created_by": plan.created_by,
                    "purpose": str(plan.purpose),
                    "valid_until": plan.valid_until.isoformat(),
                }
            )
        return {
            "items": values,
            "next_after": values[-1]["plan_id"] if len(rows) > PAGE_SIZE else None,
        }


def plan_detail(connect, lifecycle, application, plan_id):
    plan = lifecycle.get_plan(application, plan_id)
    compiled = PostgresCompiledPlans(connect, tenant_id=lifecycle.tenant_id).for_plan(
        plan
    )
    document = json.loads(compiled.canonical)
    if document["service_id"] != application:
        raise ContractError("Compiled plan belongs to another application")
    with lifecycle.transaction() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        rows = conn.execute(
            """SELECT approval_id FROM sova_control.approvals
            WHERE tenant_id=%s AND application_id=%s AND plan_id=%s
            ORDER BY created_at DESC,approval_id LIMIT 21""",
            (lifecycle.tenant_id, application, plan_id),
        ).fetchall()
        approvals = []
        for (approval_id,) in rows[:20]:
            approved = lifecycle._get_approval(conn, application, approval_id)
            approvals.append(
                {
                    "approval_id": approved.approval_id,
                    "revision": approved.revision,
                    "revoked": approved.revoked,
                    "approved_by": approved.approval.approved_by,
                    "expires_at": approved.approval.valid_until.isoformat(),
                }
            )
    return {
        "plan_id": plan.plan_id,
        "plan_digest": plan.fingerprint,
        "compiled_digest": compiled.digest,
        "plan": plan.document,
        "compiled": document,
        "unit_digest": document["unit"]["unit_digest"],
        "approvals": approvals,
        "more_approvals": len(rows) > 20,
        "authorizes_execution": False,
    }
