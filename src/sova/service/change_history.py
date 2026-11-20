"""Read-only app history, deliberately separate from plan impact assessment.

Ingestion time orders pages, not provider time. Pagination is not a snapshot:
new records require a refresh. Neither records nor an empty page prove feed
completeness, collector health, or permission to execute.
"""

import base64
import binascii
import json
from datetime import datetime

from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.contracts import ContractError, identifier

PAGE_SIZE = 20


def cursor(tenant, application, recorded_at, event_id):
    return base64.urlsafe_b64encode(json.dumps(
        [tenant, application, recorded_at.isoformat(), event_id],
        separators=(",", ":"),
    ).encode()).decode()


def position(after, tenant, application):
    if not after:
        return None
    try:
        if len(after) > 2048:
            raise ValueError
        raw = json.loads(base64.b64decode(after, altchars=b"-_", validate=True))
        if (not isinstance(raw, list) or len(raw) != 4
                or raw[:2] != [tenant, application]
                or not all(isinstance(x, str) for x in raw)):
            raise ValueError
        at = datetime.fromisoformat(raw[2])
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError
        identifier(raw[3], "event cursor")
        return at, raw[3]
    except (ValueError, TypeError, binascii.Error, ContractError):
        raise ContractError("Change history cursor is invalid for this application") from None


def read(connect, tenant, application, after=""):
    identifier(application, "application_id")
    before = position(after, tenant, application)
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    with feed.transaction() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        # A separate fixed query avoids string interpolation of cursor values.
        condition = "AND (recorded_at,event_id)<(%s,%s)" if before else ""
        params = (tenant, application, *before, PAGE_SIZE + 1) if before else (
            tenant, application, PAGE_SIZE + 1)
        rows = conn.execute(
            f"""SELECT event_id,kind,environment_id,subject,artifact_digest,
                observed_at,cause_plan_digest,cause_step_id,collector_id,recorded_at
            FROM sova_control.change_events
            WHERE tenant_id=%s AND application_id=%s {condition}
            ORDER BY recorded_at DESC,event_id DESC LIMIT %s""", params,
        ).fetchall()
        items = []
        for event_id, kind, env, subject, artifact, observed, plan, step, collector, recorded in rows[:PAGE_SIZE]:
            event = ChangeEvent(event_id, tenant, application, ChangeKind(kind), env,
                                subject, artifact, observed,
                                None if plan is None else (plan, step))
            identifier(collector, "collector_id")
            if recorded.tzinfo is None:
                raise ContractError("Change history recording time is invalid")
            items.append({"event_id": event.event_id, "kind": str(event.kind),
                          "environment_id": event.environment_id, "subject": event.subject,
                          "observed_at": event.observed_at.isoformat(),
                          "recorded_at": recorded.isoformat(), "collector_id": collector})
        return {"version": 1, "tenant_id": tenant, "application_id": application,
                "observational_only": True, "authorizes_execution": False,
                "coverage": "UNKNOWN", "items": items,
                "next_after": cursor(tenant, application, rows[PAGE_SIZE-1][-1],
                                     rows[PAGE_SIZE-1][0]) if len(rows) > PAGE_SIZE else None}
