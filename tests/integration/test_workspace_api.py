"""Real SQL/API workspace reads; identity issuer is a fixture in this suite."""

from dataclasses import replace

import psycopg
import pytest

from test_service_api import api as api  # noqa: F401
from test_service_control_plane import request_of
from test_service_control_plane import service as service  # noqa: F401
from sova.adapters.registration import registry_codec as codecs

pytestmark = pytest.mark.postgres


def register(c, i, t, value):
    kind, subject = codecs.kind_of(value), codecs.subject_of(value)
    r = c.put(
        f"/v1/tenants/{t}/registrations/{kind}/{subject}",
        headers=i.token(t, "registrar"),
        json={"document": codecs.encode(value), "expected_revision": 0},
    )
    assert r.status_code == 200, r.text


def test_workspace_registration_plan_review_and_independent_approval(api):
    c, i, w, t = api["client"], api["issuer"], api["world"], api["tenant"]
    base = f"/v1/tenants/{t}"
    who = c.get(
        base + "/identity", headers=i.token(t, "planner", subject="actual-planner")
    )
    assert who.json() == {
        "tenant_id": t,
        "subject": "actual-planner",
        "roles": ["planner"],
    }
    for value in (
        w["contract"],
        w["snapshot"],
        w["source"],
        w["target"],
        w["manifest"],
        w["qualification"],
    ):
        register(c, i, t, value)
    for value in (w["contract"], w["snapshot"], w["qualification"]):
        assert (
            c.post(
                base + "/attestations",
                headers=i.token(t, "attestor"),
                json=w["sign"](value).document,
            ).status_code
            == 201
        )
    catalog = c.get(
        base + "/registrations/service_contract", headers=i.token(t, "auditor")
    ).json()
    assert (
        catalog["items"][0]["subject_id"] == api["app"]
        and catalog["next_after"] is None
    )
    assert "document" not in catalog["items"][0]
    result = c.post(
        base + "/plans",
        headers=i.token(t, "planner", subject="actual-planner"),
        json=request_of(w).document,
    )
    assert result.status_code == 201, result.text
    plan = result.json()
    app = api["app"]
    path = base + f"/applications/{app}/plans/{plan['plan_id']}"
    read = c.get(path, headers=i.token(t, "auditor"))
    assert read.status_code == 200, read.text
    detail = read.json()
    assert detail["plan_digest"] == plan["plan_digest"]
    assert (
        detail["plan"]["created_by"] == "actual-planner"
        and detail["authorizes_execution"] is False
    )
    assert detail["approvals"] == [] and len(detail["unit_digest"]) == 64
    denied = c.post(
        path + "/approvals",
        headers=i.token(t, "approver", subject="actual-planner"),
        json={"approval_id": "self"},
    )
    assert denied.status_code == 403
    accepted = c.post(
        path + "/approvals",
        headers=i.token(t, "approver", subject="independent-approver"),
        json={"approval_id": "approval-independent"},
    )
    assert accepted.status_code == 201
    updated = c.get(path, headers=i.token(t, "operator")).json()
    assert updated["approvals"][0]["approved_by"] == "independent-approver"
    assert updated["approvals"][0]["revoked"] is False
    plans = c.get(
        base + f"/applications/{app}/plans", headers=i.token(t, "auditor")
    ).json()
    assert plans["items"][0]["plan_id"] == plan["plan_id"]
    assert c.get(path, headers=i.token("other-tenant", "auditor")).status_code == 403
    assert c.get(path, headers=i.token(t, "unknown-role")).status_code == 403


def test_workspace_catalog_is_bounded_and_paginated_without_duplicates(api):
    c, i, t = api["client"], api["issuer"], api["tenant"]
    for n in range(25):
        register(c, i, t, replace(api["world"]["source"], profile_id=f"profile-{n:02}"))
    path = f"/v1/tenants/{t}/registrations/environment_profile"
    first = c.get(path, headers=i.token(t, "auditor")).json()
    assert len(first["items"]) == 20 and first["next_after"] == "profile-19"
    second = c.get(
        path, params={"after": first["next_after"]}, headers=i.token(t, "auditor")
    ).json()
    assert len(second["items"]) == 5 and second["next_after"] is None
    assert len({r["subject_id"] for r in first["items"] + second["items"]}) == 25


def test_workspace_does_not_hide_a_broken_registry_head(api, postgres_database):
    register(api["client"], api["issuer"], api["tenant"], api["world"]["source"])
    with psycopg.connect(postgres_database[0]) as conn:
        conn.execute(
            "UPDATE sova_control.registration_heads SET revision=revision+1 WHERE tenant_id=%s",
            (api["tenant"],),
        )
    r = api["client"].get(
        f"/v1/tenants/{api['tenant']}/registrations/environment_profile",
        headers=api["issuer"].token(api["tenant"], "auditor"),
    )
    assert r.status_code == 422
