"""The control plane over HTTP: bearer tokens signed by an issuer key the
service fetches as JWKS, every route on real PostgreSQL behind it."""


import time
from fastapi.testclient import TestClient
import pytest
from test_service_control_plane import feeds_complete, request_of
from test_service_control_plane import service as service  # noqa: F401 - the fixture

from sova.adapters.registration import registry_codec as codecs
from sova.service.api import create_app
from sova.service.auth import BearerAuthenticator

from oidc_fixture import ISSUER, AUDIENCE, Issuer

pytestmark = pytest.mark.postgres


@pytest.fixture
def api(service):  # noqa: F811 - the control-plane fixture of the other module
    issuer = Issuer()
    authenticate = BearerAuthenticator(issuer=ISSUER, audience=AUDIENCE,
                                       fetch_jwks=issuer.jwks)
    client = TestClient(create_app(service["plane"], authenticate))
    return {"client": client, "issuer": issuer, **service}


def test_a_handoff_is_registered_planned_approved_and_admitted_over_http(api):
    c, i, w, t = api["client"], api["issuer"], api["world"], api["tenant"]
    base = f"/v1/tenants/{t}"
    for value in (w["contract"], w["snapshot"], w["source"], w["target"], w["manifest"],
                  w["qualification"]):
        kind, subject = codecs.kind_of(value), codecs.subject_of(value)
        r = c.put(f"{base}/registrations/{kind}/{subject}", headers=i.token(t, "registrar"),
                  json={"document": codecs.encode(value), "expected_revision": 0})
        assert r.status_code == 200, r.text
    for subject in (w["contract"], w["snapshot"], w["qualification"]):
        r = c.post(f"{base}/attestations", headers=i.token(t, "attestor"),
                   json=w["sign"](subject).document)
        assert r.status_code == 201, r.text
    request = request_of(w)
    r = c.post(f"{base}/plans", headers=i.token(t, "planner"), json={
        "service_id": request.service_id, "snapshot_id": request.snapshot_id,
        "source_profile": request.source_profile, "target_profile": request.target_profile,
        "manifest": request.manifest, "qualification": request.qualification,
        "nodes": list(request.nodes), "rows": request.rows})
    assert r.status_code == 201, r.text
    compiled = r.json()
    app_base = f"{base}/applications/{compiled['application_id']}"
    r = c.post(f"{app_base}/plans/{compiled['plan_id']}/approvals",
               headers=i.token(t, "planner", "approver", subject="planner-person"),
               json={"approval_id": "approval-1"})
    assert r.status_code == 403  # the planner who compiled it
    r = c.post(f"{app_base}/plans/{compiled['plan_id']}/approvals",
               headers=i.token(t, "approver"), json={"approval_id": "approval-1",
                                                     "valid_minutes": 30})
    assert r.status_code == 201, r.text
    feeds_complete(api, compiled)
    r = c.post(f"{app_base}/admissions", headers=i.token(t, "operator"),
               json={"approval_id": "approval-1"})
    assert r.status_code == 201, r.text
    unit = r.json()["unit_digest"]
    r = c.get(f"{app_base}/units/{unit}", headers=i.token(t, "auditor"))
    assert r.status_code == 200 and r.json()["state"] == "SOURCE_ACTIVE"
    assert r.headers["x-request-id"]
    # Nothing measured yet; an observation that does not exist is refused, and a
    # digest that is not one never reaches the database.
    r = c.get(f"{app_base}/evidence", headers=i.token(t, "auditor"))
    assert r.status_code == 200 and r.json() == {"evidence": []}
    r = c.get(f"{app_base}/observations/{'a' * 64}", headers=i.token(t, "auditor"))
    assert r.status_code == 404
    r = c.get(f"{app_base}/observations/not-a-digest", headers=i.token(t, "auditor"))
    assert r.status_code == 422


def test_tokens_are_checked_before_anything_else(api):
    c, i, w, t = api["client"], api["issuer"], api["world"], api["tenant"]
    url = f"/v1/tenants/{t}/registrations/environment_profile/{w['source'].profile_id}"
    assert c.get(url).status_code == 401
    forged = i.token(t, "auditor")["authorization"][:-8] + "AAAAAAAA"
    assert c.get(url, headers={"authorization": forged}).status_code == 401
    assert c.get(url, headers=i.token(t, "auditor", exp=int(time.time()) - 3600)
                 ).status_code == 401
    assert c.get(url, headers=i.token(t, "auditor", aud="another-service")).status_code == 401
    assert c.get(url, headers=i.token("tenant-other", "auditor")).status_code == 403
    no_role = i.token(t, "auditor")
    assert c.get(url, headers=no_role).status_code == 404  # authenticated, nothing there


def test_conflicts_and_refusals_have_their_own_status(api):
    c, i, w, t = api["client"], api["issuer"], api["world"], api["tenant"]
    url = f"/v1/tenants/{t}/registrations/environment_profile/{w['source'].profile_id}"
    body = {"document": codecs.encode(w["source"]), "expected_revision": 0}
    assert c.put(url, headers=i.token(t, "registrar"), json=body).status_code == 200
    other = dict(body, document={**body["document"], "revision": 9})
    r = c.put(url, headers=i.token(t, "registrar"), json=other)
    assert r.status_code == 409 and r.json()["current_revision"] == 1
    assert c.put(url, headers=i.token(t, "registrar"),
                 json={"document": {}, "expected_revision": 1}).status_code == 422
    assert c.put(url, headers=i.token(t, "planner"), json=body).status_code == 403
    assert c.get("/healthz").json() == {"status": "ok"}
