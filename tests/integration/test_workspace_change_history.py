"""Real isolated SQL/API/browser; change events and inventory are fixtures."""

import pytest
from test_workspace_api import register
from test_service_api import api as api  # noqa: F401
from test_service_control_plane import service as service  # noqa: F401

pytestmark = pytest.mark.postgres


def test_change_history_is_app_scoped_bounded_and_never_completeness(api):
    from datetime import UTC, datetime
    from sova.adapters.postgres_change_feed import PostgresChangeFeed
    from sova.domain.change_impact import ChangeEvent, ChangeKind

    c, issuer, tenant, app = api["client"], api["issuer"], api["tenant"], api["app"]
    register(c, issuer, tenant, api["world"]["contract"])
    feed = PostgresChangeFeed(api["connect"], tenant_id=tenant)
    # The registry may itself emit app events. Compare with actual app-only SQL,
    # including its records, rather than treating the fixture as production data.
    for n in range(25):
        feed.record(ChangeEvent(f"history-{n:02}", tenant, app, ChangeKind.SCHEMA,
                                "shared-env", "database-uid", None, datetime.now(UTC)),
                    collector_id="synthetic-collector")
    feed.record(ChangeEvent("private-other-app", tenant, "another-app", ChangeKind.POLICY,
                            "shared-env", "private-resource", None, datetime.now(UTC)),
                collector_id="synthetic-collector")
    with feed.transaction() as conn:
        expected = conn.execute("""SELECT event_id FROM sova_control.change_events
            WHERE tenant_id=%s AND application_id=%s
            ORDER BY recorded_at DESC,event_id DESC""", (tenant, app)).fetchall()
    path = f"/v1/tenants/{tenant}/applications/{app}/change-history"
    headers = issuer.token(tenant, "auditor")
    first = c.get(path, headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    one = first.json()
    assert len(one["items"]) == 20 and one["next_after"]
    two = c.get(path, headers=headers, params={"after": one["next_after"]}).json()
    assert two["next_after"] is None
    items = one["items"] + two["items"]
    assert [r["event_id"] for r in items] == [r[0] for r in expected]
    assert all(r["subject"] != "private-resource" for r in items)
    assert one["coverage"] == two["coverage"] == "UNKNOWN"
    assert one["observational_only"] is True and one["authorizes_execution"] is False
    assert all("artifact_digest" not in row and "cause_plan_digest" not in row for row in items)
    assert c.get(path, headers=issuer.token(tenant, "operator")).status_code == 403
    assert c.get(path, headers=issuer.token("other-tenant", "auditor")).status_code == 403
    assert c.get(path).status_code == 401
    assert c.get(path, headers=headers, params={"after": "invalid!"}).status_code == 422
    from sova.service.change_history import cursor
    cross = cursor(tenant, "another-app", datetime.now(UTC), "event")
    assert c.get(path, headers=headers, params={"after": cross}).status_code == 422
    missing = path.replace(f"/applications/{app}/", "/applications/unregistered/")
    assert c.get(missing, headers=headers).status_code == 404


def test_change_history_empty_does_not_mean_unchanged(api):
    c, issuer, tenant, app = api["client"], api["issuer"], api["tenant"], api["app"]
    register(c, issuer, tenant, api["world"]["contract"])
    # Cursor before this test's ingestion gives an empty historical page.
    from datetime import UTC, datetime
    from sova.service.change_history import cursor
    after = cursor(tenant, app, datetime(1970, 1, 1, tzinfo=UTC), "event")
    response = c.get(f"/v1/tenants/{tenant}/applications/{app}/change-history",
                     params={"after": after}, headers=issuer.token(tenant, "auditor"))
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [] and response.json()["next_after"] is None
    assert response.json()["coverage"] == "UNKNOWN"
    assert response.json()["authorizes_execution"] is False
