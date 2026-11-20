"""Disposable realm import for real PKCE; no bootstrap/admin password grant.

The generated password-bearing JSON goes only over stdin to the owned,
non-root container's tmpfs. It is never a fake token/JWKS or shared realm.
"""

import json
import secrets
import subprocess
from uuid import uuid4


def definition(h, *, client_id, security_roles):
    realm = "journey-" + uuid4().hex
    roles = ("registrar", "attestor", "planner", "operator", "approver", "auditor")
    security = ("trust_requester", "trust_approver") if security_roles else ()
    users, imported = {}, []
    for name, granted in (
        ("planner", (*roles, *(("trust_requester",) if security_roles else ()))),
        (
            "approver",
            ("approver", "auditor", *(("trust_approver",) if security_roles else ())),
        ),
    ):
        password, subject = secrets.token_urlsafe(24), str(uuid4())
        users[name] = {"username": name, "password": password, "subject": subject}
        imported.append(
            {
                "id": subject,
                "username": name,
                "email": name + "@example.test",
                "firstName": name.title(),
                "lastName": "Tester",
                "enabled": True,
                "emailVerified": True,
                "realmRoles": ["sova:" + role for role in granted],
                "credentials": [
                    {"type": "password", "value": password, "temporary": False}
                ],
            }
        )
    client = {
        "clientId": client_id,
        "publicClient": True,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": [h.origin + "/workspace/callback"],
        "webOrigins": [h.origin],
        "attributes": {"pkce.code.challenge.method": "S256"},
        "defaultClientScopes": [
            "web-origins",
            "acr",
            "roles",
            "profile",
            "email",
            "basic",
        ],
        "protocolMappers": [
            {
                "name": "tenant",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "config": {
                    "claim.name": "sova_tenant",
                    "claim.value": h.tenant,
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                    "id.token.claim": "true",
                },
            },
            {
                "name": "api-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": "sova-control-plane",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            },
        ],
    }
    document = {
        "realm": realm,
        "enabled": True,
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "accessTokenLifespan": 120,
        "ssoSessionIdleTimeout": 600,
        "roles": {"realm": [{"name": "sova:" + name} for name in (*roles, *security)]},
        "clients": [client],
        "users": imported,
    }
    return realm, users, document


WAIT_AND_START = (
    "set -eu; while ! test -f /opt/keycloak/data/import/.ready; do sleep 0.1; done; "
    "exec /opt/keycloak/bin/kc.sh start --optimized --http-enabled=true "
    "--hostname-strict=false --cache=local --import-realm"
)


def transfer(container, realm, document):
    # Filename components are generated here, never an uploaded path or shell text.
    if (
        not realm.startswith("journey-")
        or len(realm) != 40
        or any(c not in "0123456789abcdef" for c in realm[8:])
        or document["realm"] != realm
    ):
        raise ValueError("Owned generated realm required")
    target = "/opt/keycloak/data/import/" + realm + "-realm.json"
    command = (
        "set -eu; umask 077; mkdir -m 700 /opt/keycloak/data/import; "
        'test ! -e "$1"; cat > "$1"; chmod 400 "$1"; '
        "touch /opt/keycloak/data/import/.ready"
    )
    run = subprocess.run(
        ["docker", "exec", "-i", container, "/bin/bash", "-c", command, "--", target],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if run.returncode:
        raise RuntimeError(
            "Owned tmpfs realm transfer failed; no provisioning request replayed"
        )
