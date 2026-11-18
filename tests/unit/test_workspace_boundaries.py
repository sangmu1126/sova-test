import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sova.domain.contracts import ContractError
from sova.service.auth import BearerAuthenticator, oidc_url, Unauthenticated
from sova.service.http_boundary import RequestBodyBoundary
from sova.service.web import mount_workspace
from sova.service.api import create_app
from oidc_fixture import Issuer, ISSUER, AUDIENCE


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.attacker.test/realm",
        "http://127.0.0.10/realm",
        "http://localhost/realm",
        "https://user:password@example.test/realm",
        "https://example.test/realm?x=1",
    ],
)
def test_oidc_plain_http_exception_is_literal_loopback_only(url):
    assert not oidc_url(url)
    with pytest.raises(ContractError):
        BearerAuthenticator(issuer=url, audience="a", fetch_jwks=lambda: {})


def test_known_jwks_key_is_refreshed_and_withdrawal_is_observed():
    issuer = Issuer()
    published = [issuer.jwks()]
    clock = [1_790_000_000.0]
    calls = []
    token = issuer.token(
        "tenant-a", "auditor", iat=int(clock[0]), exp=int(clock[0]) + 300
    )["authorization"]

    def fetch():
        calls.append(clock[0])
        return published[0]

    auth = BearerAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        fetch_jwks=fetch,
        clock=lambda: clock[0],
        refresh_interval=10,
    )
    assert auth(token).tenant_id == "tenant-a"
    published[0] = {"keys": []}
    clock[0] += 11
    with pytest.raises(Unauthenticated):
        auth(token)
    assert len(calls) == 2


@pytest.mark.parametrize("overrides", [
    {"aud": 4}, {"aud": {AUDIENCE: True}}, {"aud": [AUDIENCE, {}]},
    {"nbf": "tomorrow"}, {"nbf": True}, {"realm_access": {"roles": "sova:operator"}},
    {"realm_access": {"roles": {"sova:operator": True}}},
])
def test_malformed_signed_claim_shapes_are_authentication_denials(overrides):
    issuer = Issuer()
    auth = BearerAuthenticator(issuer=ISSUER, audience=AUDIENCE, fetch_jwks=issuer.jwks)
    with pytest.raises(Unauthenticated):
        auth(issuer.token("tenant-a", "operator", **overrides)["authorization"])


@pytest.mark.parametrize("header,claims", [([], {}), ({}, []), (None, {}), ({}, None)])
def test_non_object_jose_parts_fail_before_key_lookup(header, claims):
    import json
    from oidc_fixture import b64

    def key_lookup():
        raise AssertionError("Malformed input must not contact the IdP")

    token = ".".join((b64(json.dumps(header).encode()), b64(json.dumps(claims).encode()), "AA"))
    auth = BearerAuthenticator(issuer=ISSUER, audience=AUDIENCE, fetch_jwks=key_lookup)
    with pytest.raises(Unauthenticated):
        auth("Bearer " + token)


@pytest.mark.parametrize(
    "headers,chunks,status",
    [
        ([], [b"123", b"456"], 413),
        ([(b"content-length", b"4"), (b"content-length", b"4")], [b"1234"], 413),
        ([(b"content-length", b"5")], [b"123"], 400),
        ([], [b"123", b"45"], 200),
    ],
)
def test_body_is_bounded_before_inner_app(headers, chunks, status):
    seen = []
    sent = []
    events = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def inner(scope, receive, send):
        seen.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return events.pop(0)

    async def send(event):
        sent.append(event)

    asyncio.run(
        RequestBodyBoundary(inner, limit=5)(
            {"type": "http", "headers": headers}, receive, send
        )
    )
    assert sent[0]["status"] == status
    assert seen == ([b"12345"] if status == 200 else [])


def bundle(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("export {}")
    (tmp_path / "index.html").write_text("legacy")
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/index.html").write_text("workspace")
    return tmp_path


def test_workspace_public_assets_are_same_origin_and_do_not_grant_api_access(tmp_path):
    def authenticate(_):
        raise Unauthenticated("A bearer token is required")

    app = create_app(SimpleNamespace(), authenticate)
    mount_workspace(
        app,
        root=bundle(tmp_path),
        origin="http://127.0.0.1:8080",
        issuer="https://idp.example.test/realm",
        client_id="sova",
        tenant_id="tenant-a",
        audience="api",
    )
    c = TestClient(app, base_url="http://127.0.0.1:8080")
    r = c.get("/workspace/config")
    assert r.status_code == 200
    assert set(r.json()) == {
        "version",
        "issuer",
        "client_id",
        "tenant_id",
        "audience",
        "origin",
        "redirect_uri",
        "post_logout_redirect_uri",
    }
    assert r.headers["cache-control"] == "no-store"
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert c.get("/workspace/").text == "workspace"
    assert c.get("/console/assets/app.js").status_code == 200
    assert c.get("/console/assets/secret.key").status_code == 404
    assert (
        c.get("/workspace/config", headers={"Host": "attacker.invalid"}).status_code
        == 422
    )
    assert c.get("/v1/tenants/tenant-a/identity").status_code == 401


def test_workspace_rejects_remote_plaintext_configuration(tmp_path):
    with pytest.raises(ContractError):
        mount_workspace(
            FastAPI(),
            root=bundle(tmp_path),
            origin="http://public.example.test",
            issuer="https://idp.example.test/realm",
            client_id="sova",
            tenant_id="tenant-a",
            audience="api",
        )
