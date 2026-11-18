"""Actual DB admission refusal and real TCP response hints, not a cutover."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from psycopg import sql
import psycopg
import pytest
from postgres_business import Route
from qualification_orders import application, serve
from test_order_http_handoff import http_business as http_business  # noqa: F401

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


@pytest.mark.parametrize("hint", [None, 1])
def test_real_refusal_has_optional_wait_hint_but_never_becomes_authoritative_absence(http_business, hint):
    b = http_business
    scope = {"tenant_id": "tenant-" + uuid4().hex, "run_id": "backpressure-" + uuid4().hex}
    token = uuid4().hex
    headers = {"authorization": "Bearer " + token, "x-qualification-tenant": scope["tenant_id"],
               "x-qualification-run": scope["run_id"], "idempotency-key": "one-order"}
    body, params = {"customer": "backpressure-customer", "amount": "12.34"}, {
        "customer": "backpressure-customer", "idempotency_key": "one-order"}
    with b["connect_as"](b["names"]["src"], b["app"]) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    with b["connect"](b["names"]["src"]) as conn:
        conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC, {}").format(
            sql.Identifier(b["names"]["src"]), sql.Identifier(b["app"])))
        assert conn.execute("SELECT has_database_privilege(%s,%s,'CONNECT')",
                            (b["app"], b["names"]["src"])).fetchone() == (False,)
    # libpq startup failures may expose OperationalError without SQLSTATE;
    # don't invent a query-error classification for a connection rejection.
    with pytest.raises(psycopg.Error) as denied:
        b["connect_as"](b["names"]["src"], b["app"])
    app = application(b, Route(b["names"]["src"]), scope, token, lookup_retry_after_seconds=hint)
    with serve(app) as origin, httpx.Client(base_url=origin, headers=headers, trust_env=False,
                                           follow_redirects=False, timeout=2) as client:
        post = client.post("/orders", json=body)
        lookup = client.get("/orders", params=params)
        assert post.status_code == lookup.status_code == 503
        assert post.headers["retry-after"] == "1"
        assert lookup.headers.get("retry-after") == ("1" if hint == 1 else None)
        assert post.json() == lookup.json() == {"error": "temporarily unavailable"}
        unauthenticated = client.get("/orders", params=params, headers={"authorization": "Bearer wrong"})
        wrong_tenant = client.get("/orders", params=params, headers={"x-qualification-tenant": "another"})
        assert unauthenticated.status_code == 401 and wrong_tenant.status_code == 403
        assert "retry-after" not in unauthenticated.headers and "retry-after" not in wrong_tenant.headers
        with b["connect"](b["names"]["src"]) as conn:
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(b["names"]["src"]), sql.Identifier(b["app"])))
        missing = client.get("/orders", params=params)
        assert missing.status_code == 404 and "retry-after" not in missing.headers
        first, repeated = client.post("/orders", json=body), client.post("/orders", json=body)
        assert first.status_code == repeated.status_code == 200 and first.json() == repeated.json()
        assert client.get("/orders", params=params).json() == first.json()
        conflict = client.post("/orders", json={**body, "amount": "99"})
        assert conflict.status_code == 409 and "retry-after" not in conflict.headers
    with b["connect"](b["names"]["src"]) as conn:
        assert conn.execute("SELECT count(*) FROM orders WHERE customer='backpressure-customer'").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM orders WHERE customer LIKE 'customer-%'").fetchone() == (50,)
    if directory := os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS"):
        (Path(directory) / ("lookup-contract-legacy.json" if hint is None else "lookup-contract-hinted.json")).write_text(
            json.dumps({"app_positive_control_before_refusal": True, "actual_connect_privilege_while_refused": False,
                "connect_refusal_exception_class": type(denied.value).__name__, "connect_refusal_sqlstate": denied.value.sqlstate,
                "post_status": post.status_code,
                "post_retry_after": post.headers.get("retry-after"), "lookup_status": lookup.status_code,
                "lookup_retry_after": lookup.headers.get("retry-after"), "unauthenticated": 401,
                "wrong_tenant": 403, "missing_after_restored_access": 404, "business_conflict": 409,
                "logical_orders_after_repeated_submit": 1, "historical_orders": 50,
                "qualification_issued": False, "authorizes_execution": False}, indent=2) + "\n")
