"""Synthetic signed OIDC producer for API unit/integration tests only."""

import base64
import json
import time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ISSUER, AUDIENCE = "https://idp.example.test/realms/sova", "sova-control-plane"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Issuer:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self):
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "k1",
                    "use": "sig",
                    "alg": "RS256",
                    "n": b64(numbers.n.to_bytes(256, "big")),
                    "e": b64(numbers.e.to_bytes(3, "big")),
                }
            ]
        }

    def token(self, tenant, *roles, subject=None, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject or f"{roles[0]}-person",
            "sova_tenant": tenant,
            "iat": now,
            "exp": now + 300,
            "realm_access": {
                "roles": [f"sova:{r}" for r in roles] + ["offline_access"]
            },
            **overrides,
        }
        head = b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        signature = self.key.sign(
            f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return {"authorization": f"Bearer {head}.{body}.{b64(signature)}"}
