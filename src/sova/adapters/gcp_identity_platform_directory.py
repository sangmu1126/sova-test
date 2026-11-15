"""Google Cloud Identity Platform as an identity migration source (Part II §5-§7).

Reads only, with one call: ``accounts:batchGet`` of the project or of one
tenant. The response can carry password hashes and salts (Identity Platform's
modified scrypt, which the target cannot verify); they are dropped as each
page arrives and never stored, logged or compared. Moving out of Identity
Platform therefore means a new credential at the target
(``RESET_AND_REENROLL``), as with Cognito.

The stable subject is ``localId``: the user's immutable UID, which is the
``sub`` of the ID tokens Identity Platform issues. The e-mail address can
change and is never used to merge accounts. Identity Platform has no user
names; the address (lower case) is the login name at the target.

Groups come from the ``groups`` custom claim, the usual place applications
keep them. A user who has no password sign-in and signs in only through
another provider (Google, SAML, OIDC) is reported as federated: a password
reset cannot move it.
"""

import json
import re

from sova.adapters.gcp_rest import GcpRest
from sova.adapters.keycloak_identity import AccountBoundExceeded
from sova.domain.contracts import ContractError

API = "https://identitytoolkit.googleapis.com/v1"
_TENANT = re.compile(r"[A-Za-z][A-Za-z0-9-]{3,39}")
_DROPPED = frozenset({"passwordHash", "salt", "mfaInfo", "lastRefreshAt", "validSince"})
PAGE = 500


def _groups(user):
    raw = user.get("customAttributes")
    if not raw:
        return []
    try:
        claims = json.loads(raw)
    except ValueError:
        raise ContractError("Identity Platform user with unreadable custom claims") from None
    groups = claims.get("groups") or []
    if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
        raise ContractError("The groups claim must be a list of names")
    return sorted("/" + g.lstrip("/") for g in groups)


class IdentityPlatformDirectory:
    def __init__(self, rest, *, tenant=None):
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        if tenant is not None and (type(tenant) is not str or not _TENANT.fullmatch(tenant)):
            raise ContractError("Identity Platform tenant ID required")
        self.rest, self.tenant = rest, tenant
        base = f"{API}/projects/{rest.project}"
        self.url = base + (f"/tenants/{tenant}" if tenant else "") + "/accounts:batchGet"

    @property
    def issuer(self):
        return f"https://securetoken.google.com/{self.rest.project}"

    def accounts(self, bound):
        accounts, token = [], None
        while True:
            params = {"maxResults": PAGE, **({"nextPageToken": token} if token else {})}
            page = self.rest.get(self.url, params=params) or {}
            for user in page.get("users", ()):
                for field in _DROPPED & user.keys():
                    user[field] = None  # never kept past this page
                if len(accounts) >= bound:
                    raise AccountBoundExceeded("More Identity Platform users than the bound")
                if not user.get("localId"):
                    raise ContractError("Identity Platform user without a UID")
                email = (user.get("email") or "").lower() or None
                if email is None:
                    raise ContractError("Identity Platform user without an e-mail address")
                providers = {p.get("providerId") for p in user.get("providerUserInfo", ())}
                name = (user.get("displayName") or "").strip()
                first, _, last = name.partition(" ")
                accounts.append({
                    "subject": user["localId"], "username": email, "email": email,
                    "firstName": first or None, "lastName": last or None,
                    "enabled": not user.get("disabled", False),
                    "emailVerified": bool(user.get("emailVerified")),
                    "groups": _groups(user),
                    "federated": bool(providers) and "password" not in providers})
            token = page.get("nextPageToken")
            if not token or not page.get("users"):
                return accounts
