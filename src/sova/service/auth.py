"""Bearer tokens from the organisation's OIDC provider, turned into principals.

Every request carries an access token issued by the configured issuer. It is
accepted only if its RS256 signature verifies against a key the issuer
publishes (JWKS, cached and refreshed when an unknown key ID appears, at most
once per ``refresh_interval``), it names this service as audience, and it is
inside its lifetime (bounded clock skew). The principal is the token's
``sub``; its tenant and roles come from claims the provider is configured to
issue (``tenant_claim``, and roles as ``<role_prefix><role>`` in
``roles_claim``, e.g. realm roles ``sova:planner``). Anything else, an
unknown role included, grants nothing.
"""

import base64
import json
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from sova.domain.contracts import ContractError
from sova.service.control_plane import Principal, Role

SKEW = 60


class Unauthenticated(ContractError):
    pass


class AuthenticationUnavailable(ContractError):
    """No new principal can be verified while its configured provider fails."""


def _b64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _int(value):
    return int.from_bytes(_b64(value), "big")


def _claim(claims, path):
    value = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class BearerAuthenticator:
    def __init__(self, *, issuer, audience, fetch_jwks, tenant_claim="sova_tenant",
                 roles_claim="realm_access.roles", role_prefix="sova:",
                 refresh_interval=60.0, clock=time.time):
        if not issuer.startswith("https://") and not issuer.startswith("http://127.0.0.1"):
            raise ContractError("An HTTPS issuer is required")
        self.issuer, self.audience, self.fetch_jwks = issuer, audience, fetch_jwks
        self.tenant_claim, self.roles_claim, self.role_prefix = (
            tenant_claim, roles_claim, role_prefix)
        self.refresh_interval, self.clock = refresh_interval, clock
        self._keys, self._fetched, self._lock = {}, float("-inf"), threading.Lock()

    def _key(self, kid):
        with self._lock:
            if kid not in self._keys and self.clock() - self._fetched >= self.refresh_interval:
                published = self.fetch_jwks()
                self._keys = {k["kid"]: rsa.RSAPublicNumbers(_int(k["e"]), _int(k["n"]))
                              .public_key() for k in published.get("keys", ())
                              if k.get("kty") == "RSA" and k.get("kid")
                              and k.get("use", "sig") == "sig"}
                self._fetched = self.clock()
            return self._keys.get(kid)

    def __call__(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise Unauthenticated("A bearer token is required")
        token = authorization[len("Bearer "):].strip()
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
            header, claims = json.loads(_b64(header_b64)), json.loads(_b64(payload_b64))
            signature = _b64(signature_b64)
        except (ValueError, UnicodeError):
            raise Unauthenticated("Malformed token") from None
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise Unauthenticated("Only RS256 tokens with a key ID are accepted")
        key = self._key(header["kid"])
        if key is None:
            raise Unauthenticated("The token's key is not published by the issuer")
        try:
            key.verify(signature, f"{header_b64}.{payload_b64}".encode(), padding.PKCS1v15(),
                       hashes.SHA256())
        except InvalidSignature:
            raise Unauthenticated("The token's signature does not verify") from None
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else list(audience or ())
        if claims.get("iss") != self.issuer or self.audience not in audience:
            raise Unauthenticated("The token was issued by or for someone else")
        now, expires, issued = self.clock(), claims.get("exp"), claims.get("iat")
        if (type(expires) is not int or type(issued) is not int or expires <= now - SKEW
                or issued > now + SKEW or claims.get("nbf", issued) > now + SKEW):
            raise Unauthenticated("The token is outside its lifetime")
        tenant = claims.get(self.tenant_claim)
        granted = _claim(claims, self.roles_claim) or ()
        roles = frozenset(Role(r[len(self.role_prefix):]) for r in granted
                          if isinstance(r, str) and r.startswith(self.role_prefix)
                          and r[len(self.role_prefix):] in {x.value for x in Role})
        try:
            return Principal(claims.get("sub"), tenant, roles)
        except ContractError:
            raise Unauthenticated("The token names no valid subject or tenant") from None
