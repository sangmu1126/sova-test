"""ID token verification against a locally generated key and a mock issuer.

The real-provider tests sign in at Keycloak; these cover the cases a correct
provider never produces: a validly signed token with the wrong issuer,
audience or lifetime, an unsigned token, an unknown key.
"""

import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest

httpx = pytest.importorskip("httpx")

from sova.adapters.oidc_probes import OidcProbes  # noqa: E402
from sova.domain.contracts import ContractError  # noqa: E402

ISSUER = "https://idp.example.test/realms/orders"
NOW = 1_800_000_000
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _uint(value):
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def transport(issuer=ISSUER, kid="k1"):
    numbers = KEY.public_key().public_numbers()

    def handle(request):
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": issuer, "jwks_uri": issuer + "/certs",
                "token_endpoint": issuer + "/token"})
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [
                {"kty": "RSA", "kid": kid, "n": _uint(numbers.n), "e": _uint(numbers.e)}]})
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def token(*, alg="RS256", kid="k1", key=KEY, **claims):
    body = {"iss": ISSUER, "aud": "orders-web", "sub": "s-1", "iat": NOW - 10,
            "exp": NOW + 300, **claims}
    head = _b64(json.dumps({"alg": alg, "kid": kid}).encode())
    payload = _b64(json.dumps(body).encode())
    signature = key.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64(signature)}"


def probes(**kwargs):
    return OidcProbes(client_id="orders-web", clock=lambda: NOW,
                      transport=kwargs.pop("transport", transport()), **kwargs)


def test_a_valid_token_verifies():
    assert probes().verify(ISSUER, token())["sub"] == "s-1"
    assert probes().verify(ISSUER, token(aud=["other", "orders-web"]))["sub"] == "s-1"


@pytest.mark.parametrize("claims", [
    {"iss": "https://idp.example.test/realms/other"},  # signed, but someone else's
    {"aud": "another-client"},
    {"exp": NOW - 120},
    {"iat": NOW + 600},
    {"exp": None},
    {"iat": "yesterday"},
])
def test_a_correctly_signed_token_with_wrong_claims_is_refused(claims):
    with pytest.raises(ContractError):
        probes().verify(ISSUER, token(**claims))


def test_unsigned_unknown_key_and_other_signer_are_refused():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ContractError):
        probes().verify(ISSUER, token(key=other))
    with pytest.raises(ContractError):
        probes().verify(ISSUER, token(kid="k2"))
    with pytest.raises(ContractError):
        probes().verify(ISSUER, token(alg="HS256"))
    _, payload, _ = token().split(".")
    unsigned = _b64(b'{"alg": "none"}')
    with pytest.raises(ContractError):
        probes().verify(ISSUER, f"{unsigned}.{payload}.")
    with pytest.raises(ContractError):
        probes().verify(ISSUER, "not-a-token")


def test_discovery_must_name_the_issuer_asked_for():
    impostor = probes(transport=transport(issuer="https://evil.example.test/realms/orders"))
    with pytest.raises(ContractError):
        impostor.verify(ISSUER, token())


def test_an_unreachable_issuer_fails_closed():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    down = probes(transport=httpx.MockTransport(refuse))
    with pytest.raises(ContractError):
        down.verify(ISSUER, token())
    assert down.login(ISSUER, "alice", "pw") == (None, "unreachable")
    # Unreachable is never mistaken for a refusal: the denial probe fails.
    assert down.cross_tenant_denied(ISSUER, "mallory", "pw")[0] is False
