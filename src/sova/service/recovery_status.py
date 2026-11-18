"""Read-only operational projection; never an execution or recovery permit.

The authority record remains authoritative. UNKNOWN may survive a crashed
worker before it records SAFE_HOLD, so expose the effect barrier separately.
"""

from datetime import timedelta

from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, digest, identifier
from sova.domain.handoff_authority import record_from_document


PENDING = {"prepared", "in_flight", "unknown"}
MAX_EFFECTS = 256
NEXT_STEPS = ["VERIFY_REMOTE_SETTLEMENT", "READBACK_EXISTING_COMMAND", "REVIEW_RECOVERY_PLAN"]
VERSION = "handoff-recovery-status-v2"


def security_projection(hold_ids=(), operation_hold=None):
    return {"active_count": len(hold_ids), "hold_ids": list(hold_ids),
            "operation_hold": operation_hold}


def next_steps(reason, security):
    held = security["active_count"] > 0 or security["operation_hold"]
    steps = ["REVIEW_SECURITY_HOLD"] if held else []
    if reason not in (None, "SECURITY_HOLD", "OBSERVATION_UNAVAILABLE"):
        steps += NEXT_STEPS
    return steps


def recovery_projection(*, state, effects, mode, lease_live, security_hold=False,
                        other_pending=False, hold_ids=()):
    """Pure classification of a single database observation, not permission."""
    reason = None
    if any(e["status"] == "unknown" for e in effects):
        reason = "EFFECT_RESULT_UNKNOWN"
    elif effects and mode == "reconcile":
        reason = "RECOVERY_PENDING"
    elif effects and not lease_live:
        reason = "OWNER_EXPIRED_UNSETTLED"
    elif effects and state in {"HANDOFF_COMPLETE", "ABORTED"}:
        reason = "TERMINAL_WITH_PENDING_EFFECTS"
    elif other_pending:
        reason = "APPLICATION_PENDING_EFFECTS"
    elif security_hold or hold_ids:
        reason = "SECURITY_HOLD"
    elif state in {"SAFE_HOLD", "FAILBACK_REQUIRED"}:
        reason = "AUTHORITY_HOLD"
    security = security_projection(hold_ids, security_hold)
    return {"status": "required" if reason else "clear", "required": bool(reason),
            "reason": reason, "pending_effects": effects,
            "security_holds": security, "next_steps": next_steps(reason, security),
            "automatic_replay_allowed": False, "authorizes_execution": False}


def unavailable(hold_ids=()):
    security = security_projection(hold_ids)
    return {"status": "unavailable", "required": None, "reason": "OBSERVATION_UNAVAILABLE",
            "pending_effects": [], "security_holds": security,
            "next_steps": next_steps("OBSERVATION_UNAVAILABLE", security), "automatic_replay_allowed": False,
            "authorizes_execution": False}


def read_status(store, lifecycle, application_id, unit_digest):
    """One tenant, witnessed authority lock, then one effects/lease SQL snapshot.

READ COMMITTED is intentional: acquire the authority's shared advisory lock
before taking its row snapshot (external witness append precedes SQL commit).
No writes, FOR UPDATE, token, command parameters or connection strings escape.
"""
    identifier(application_id, "application_id")
    digest(unit_digest)
    with store.transaction() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        conn.execute("SET LOCAL lock_timeout='1s'")
        record = store._current(conn, application_id, unit_digest, lock=False)
        if record is None:
            return {"state": None}
        rows = conn.execute(
            """SELECT revision,action,actor_id,document
            FROM sova_control.handoff_authority_events
            WHERE tenant_id=%s AND application_id=%s AND unit_digest=%s
            ORDER BY event_id""", (store.tenant_id, application_id, unit_digest)).fetchall()
        history = [{"revision": rev, "action": action, "actor": actor,
                    "state": str(record_from_document(doc).state)}
                   for rev, action, actor, doc in rows]
        # A left join retains an operation with no pending effects. The owner
        # belongs to exactly this operation, never a later app operation.
        result = conn.execute(
            """WITH latest_holds AS (
                SELECT DISTINCT ON (hold_id) hold_id,revision,digest,document,event_kind
                FROM sova_control.security_hold_events
                WHERE tenant_id=%s AND application_id=%s ORDER BY hold_id,revision DESC
            ), active_holds AS (
                SELECT hold_id,revision,digest,document FROM latest_holds
                WHERE event_kind='opened' ORDER BY hold_id LIMIT 257
            ), hold_view AS (
                SELECT coalesce(jsonb_agg(jsonb_build_array(hold_id,revision,digest,document)
                    ORDER BY hold_id),'[]'::jsonb) AS documents FROM active_holds
            ) SELECT o.operation_id,o.plan_id,o.security_hold,x.mode,x.epoch,
                x.valid_until > statement_timestamp(),
                e.fingerprint,e.command_document,e.status,e.revision,
                e.environment_id,e.resource_id,
                EXISTS (SELECT 1 FROM sova_control.effects other
                    WHERE other.tenant_id=o.tenant_id AND other.application_id=o.application_id
                    AND other.operation_id<>o.operation_id
                    AND other.status IN ('prepared','in_flight','unknown')),
                statement_timestamp(),h.documents
            FROM hold_view h LEFT JOIN sova_control.operations o
                ON o.tenant_id=%s AND o.application_id=%s AND o.plan_digest=%s
            LEFT JOIN sova_control.app_execution x ON x.tenant_id=o.tenant_id
                AND x.application_id=o.application_id AND x.operation_id=o.operation_id
            LEFT JOIN sova_control.effects e ON e.tenant_id=o.tenant_id
                AND e.application_id=o.application_id AND e.operation_id=o.operation_id
                AND e.status IN ('prepared','in_flight','unknown')
            ORDER BY o.operation_id,e.fingerprint LIMIT %s""",
            (store.tenant_id, application_id, store.tenant_id, application_id,
             record.plan_digest, MAX_EFFECTS + 1)).fetchall()
        observed = result[0][13]
        hold_rows = result[0][14]
        if len(hold_rows) > 256:
            raise ContractError("Active security hold limit exceeded")
        # Reuse the existing document digest, tenant/app and domain validation.
        # The documents themselves never leave this process.
        holds = PostgresSecurityHolds(store.connect, tenant_id=store.tenant_id)
        hold_ids = tuple(holds._decode_row(application_id, row).hold.hold_id for row in hold_rows)
        recovery, operation_id = unavailable(hold_ids), None
        if (result[0][0] is not None and len(result) <= MAX_EFFECTS
                and len({r[0] for r in result}) == 1):
            first = result[0]
            operation_id = identifier(first[0], "operation_id")
            plan = lifecycle._get_plan(conn, application_id, first[1])
            if plan.fingerprint != record.plan_digest:
                raise ContractError("Status plan binding mismatch")
            pending = []
            for row in result:
                if row[6] is None:
                    continue
                command = EffectCommand.from_document(row[7])
                if (command.fingerprint, command.scope.tenant_id, command.scope.application_id,
                    command.operation_id, command.plan_digest, command.scope.environment_id,
                    command.scope.resource_id) != (row[6], store.tenant_id, application_id,
                    operation_id, record.plan_digest, row[10], row[11]):
                    raise ContractError("Status effect integrity mismatch")
                status = str(EffectStatus(row[8]))
                pending.append({"fingerprint": command.fingerprint, "action": command.action,
                                "status": status, "revision": row[9]})
            observed = first[13]
            recovery = recovery_projection(state=str(record.state), effects=pending,
                mode=first[3], lease_live=first[5] is True, security_hold=first[2],
                other_pending=first[12], hold_ids=hold_ids)
        return {"state": str(record.state), "epoch": record.epoch,
                "owner_environment": record.owner_environment,
                "next_operation": record.next_operation, "reason": record.reason,
                "activation_possible": record.activation_possible, "history": history,
                "observation_version": VERSION,
                "scope": {"tenant_id": store.tenant_id, "application_id": application_id,
                          "unit_digest": unit_digest, "plan_digest": record.plan_digest,
                          "operation_id": operation_id},
                "observed_at": observed.isoformat(),
                "expires_at": (observed + timedelta(seconds=5)).isoformat(),
                "recovery": recovery}
