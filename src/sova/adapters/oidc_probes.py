"""Independent OIDC probes for identity obligations (Part II §9).

The probes sign in for real at an issuer's token endpoint and verify what
comes back the way a relying party must: the ID token's RS256 signature
against the issuer's published keys, its issuer, audience and lifetime.
Nothing here trusts the migration adapter's view of the target. Each probe
returns ``(passed, observation)``; the observation carries no token, password
or secret, only what was decided and why, so a signed evidence record can be
taken over it.

The sign-in uses the resource-owner password grant of a test client, which a
production identity provider should not enable for real users; it is how a
synthetic test account proves the flow end to end.
"""

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import httpx

from sova.domain.contracts import ContractError

_SKEW = 60  # seconds of clock difference tolerated on iat/exp


def _b64(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _int(segment):
    return int.from_bytes(_b64(segment), "big")


class OidcProbes:
    def __init__(self, *, client_id, timeout=10.0, clock=time.time, transport=None):
        self.client_id, self.timeout, self.clock = client_id, timeout, clock
        self.transport = transport  # an httpx transport; the network when None

    def _http(self):
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    # -- protocol --------------------------------------------------------------
    def _discover(self, http, issuer):
        document = http.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        document.raise_for_status()
        config = document.json()
        if config.get("issuer") != issuer:
            raise ContractError("Discovery names a different issuer")
        return config

    def _token(self, issuer, data):
        """The token response, or the OAuth error code when refused; None when
        the issuer cannot be reached at all."""
        try:
            with self._http() as http:
                config = self._discover(http, issuer)
                response = http.post(config["token_endpoint"],
                                     data={"client_id": self.client_id, **data})
        except (httpx.TransportError, httpx.HTTPStatusError):
            return None, "unreachable"
        except ContractError:
            return None, "discovery_names_another_issuer"
        if response.status_code == 200:
            return response.json(), None
        try:
            return None, str(response.json().get("error", response.status_code))
        except ValueError:
            return None, str(response.status_code)

    def login(self, issuer, username, password):
        return self._token(issuer, {"grant_type": "password", "username": username,
                                    "password": password, "scope": "openid"})

    def refresh(self, issuer, refresh_token):
        return self._token(issuer, {"grant_type": "refresh_token",
                                    "refresh_token": refresh_token})

    def verify(self, issuer, id_token):
        """Claims of a verified ID token; ContractError when anything is off."""
        try:
            header_b64, payload_b64, signature_b64 = id_token.split(".")
            header, claims = json.loads(_b64(header_b64)), json.loads(_b64(payload_b64))
        except ValueError as error:
            raise ContractError("Malformed ID token") from error
        if header.get("alg") != "RS256":
            raise ContractError("Only RS256 ID tokens are accepted")
        try:
            with self._http() as http:
                keys = http.get(self._discover(http, issuer)["jwks_uri"])
                keys.raise_for_status()
        except httpx.HTTPError as error:
            raise ContractError("Issuer keys unavailable") from error
        key = next((k for k in keys.json().get("keys", ())
                    if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"), None)
        if key is None:
            raise ContractError("Signing key not published by the issuer")
        public = rsa.RSAPublicNumbers(_int(key["e"]), _int(key["n"])).public_key()
        try:
            public.verify(_b64(signature_b64), f"{header_b64}.{payload_b64}".encode(),
                          padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as error:
            raise ContractError("ID token signature does not verify") from error
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else list(audience or ())
        now = self.clock()
        if claims.get("iss") != issuer or self.client_id not in audience:
            raise ContractError("ID token issued by or for someone else")
        issued, expires = claims.get("iat"), claims.get("exp")
        if (type(issued) is not int or type(expires) is not int
                or issued > now + _SKEW or expires <= now - _SKEW):
            raise ContractError("ID token outside its lifetime")
        return claims

    def _signed_in(self, issuer, username, password):
        tokens, error = self.login(issuer, username, password)
        if tokens is None:
            return None, error
        try:
            return (tokens, self.verify(issuer, tokens["id_token"])), None
        except (ContractError, KeyError) as failure:
            return None, f"verification failed: {failure}"

    # -- probes ----------------------------------------------------------------
    def fresh_login(self, issuer, username, password):
        """IAM-02: a new sign-in at ``issuer`` yields a verified ID token."""
        result, error = self._signed_in(issuer, username, password)
        return result is not None, {"probe": "fresh_login", "issuer": issuer,
                                    "outcome": "verified" if result else error,
                                    "subject": result[1]["sub"] if result else None}

    def fresh_refresh(self, issuer, username, password):
        """IAM-02: a refresh token from ``issuer`` is redeemed there for a newly
        verified ID token."""
        result, error = self._signed_in(issuer, username, password)
        if result is None:
            return False, {"probe": "fresh_refresh", "issuer": issuer, "outcome": error}
        refreshed, error = self.refresh(issuer, result[0]["refresh_token"])
        if refreshed is None:
            return False, {"probe": "fresh_refresh", "issuer": issuer, "outcome": error}
        try:
            claims = self.verify(issuer, refreshed["id_token"])
        except (ContractError, KeyError) as failure:
            return False, {"probe": "fresh_refresh", "issuer": issuer,
                           "outcome": f"verification failed: {failure}"}
        return claims["sub"] == result[1]["sub"], {
            "probe": "fresh_refresh", "issuer": issuer, "outcome": "verified",
            "same_subject": claims["sub"] == result[1]["sub"]}

    def refresh_not_portable(self, source, target, username, password):
        """A refresh token from the source is refused by the target (the reason
        the contract may never assume portability, §8.1)."""
        tokens, error = self.login(source, username, password)
        if tokens is None:
            return False, {"probe": "refresh_not_portable", "outcome": f"source: {error}"}
        refreshed, error = self.refresh(target, tokens["refresh_token"])
        return refreshed is None and error != "unreachable", {
            "probe": "refresh_not_portable",
            "outcome": "accepted" if refreshed is not None else error}

    def cross_tenant_denied(self, issuer, username, password):
        """IAM-04: another tenant's account cannot sign in at this issuer."""
        tokens, error = self.login(issuer, username, password)
        return tokens is None and error != "unreachable", {
            "probe": "cross_tenant_denied", "issuer": issuer,
            "outcome": "signed_in" if tokens is not None else error}

    def permission_equivalence(self, source, target, username, source_password,
                               target_password, claim="groups"):
        """IAM-04: the same person carries the same permission claim at both."""
        observed = {}
        for side, issuer, password in (("source", source, source_password),
                                       ("target", target, target_password)):
            result, error = self._signed_in(issuer, username, password)
            if result is None:
                return False, {"probe": "permission_equivalence", side: error}
            observed[side] = sorted(result[1].get(claim) or [])
        return observed["source"] == observed["target"], {
            "probe": "permission_equivalence", "claim": claim, **observed}

    def source_blocked_login(self, source, target, username, source_password,
                             target_password):
        """IAM-08: with the source identity provider refusing sign-ins, the
        target still signs the user in."""
        blocked, source_error = self.login(source, username, source_password)
        result, target_error = self._signed_in(target, username, target_password)
        return blocked is None and result is not None, {
            "probe": "source_blocked_login",
            "source": "signed_in" if blocked is not None else source_error,
            "target": "verified" if result is not None else target_error}
