"""Independent account-writer probes for a Keycloak realm.

Each probe really tries to create an account through one writer path and
removes what it created; it shares no code with the handoff adapter, so a
defect in the adapter's reading of the realm settings does not repeat here.

* ``can_register()`` - submits the realm's self-registration form;
* ``can_manage_users(client_id)`` - a back-office client signs in with its
  own credentials and asks the admin API to create a user.

Each returns ``True`` (an account was created, then removed), ``False``
(refused) or ``None`` (neither: e.g. the form was rejected for another
reason). Only ``False`` counts as denied.
"""

import base64
import hashlib
import html
import re
import secrets
from uuid import uuid4

import httpx

from sova.adapters.keycloak_identity import KeycloakAdmin
from sova.domain.contracts import ContractError

_FORM = re.compile(r'<form[^>]*id="kc-register-form"[^>]*action="([^"]+)"')


class KeycloakWriterProbes:
    def __init__(self, admin, *, writer_secrets=None, timeout=10.0):
        if not isinstance(admin, KeycloakAdmin):
            raise ContractError("Realm administration required for cleanup")
        self.admin, self.timeout = admin, timeout
        self.writer_secrets = dict(writer_secrets or {})

    def _remove(self, username):
        with self.admin.session() as session:
            for user in session.get("/users", username=username, exact="true"):
                session.send("DELETE", f"/users/{user['id']}")

    def can_register(self):
        base, realm = self.admin.base, self.admin.realm
        username = "sova-probe-" + uuid4().hex[:12]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            page = client.get(f"{base}/realms/{realm}/protocol/openid-connect/registrations",
                              params={"client_id": "account-console", "response_type": "code",
                                      "scope": "openid",
                                      "redirect_uri": f"{base}/realms/{realm}/account/",
                                      "code_challenge": challenge,
                                      "code_challenge_method": "S256"})
            form = _FORM.search(page.text) if page.status_code == 200 else None
            if form is None:
                return False, {"path": "self_registration", "outcome": "no_form",
                               "status": page.status_code}
            # The session cookies are marked Secure; a loopback test server
            # speaks plain HTTP, so they are sent explicitly.
            cookies = "; ".join(h.split(";", 1)[0] for h in page.headers.get_list("set-cookie"))
            password = secrets.token_urlsafe(18) + "!aA1"
            submitted = client.post(html.unescape(form.group(1)), headers={"Cookie": cookies},
                                    data={"username": username,
                                          "email": f"{username}@example.invalid",
                                          "firstName": "Sova", "lastName": "Probe",
                                          "password": password, "password-confirm": password})
        with self.admin.session() as session:
            created = bool(session.get("/users", username=username, exact="true"))
        if created:
            self._remove(username)
            return True, {"path": "self_registration", "outcome": "created_and_removed"}
        return None, {"path": "self_registration", "outcome": "form_not_accepted",
                      "status": submitted.status_code}

    def can_manage_users(self, client_id):
        secret = self.writer_secrets.get(client_id)
        if secret is None:
            return None, {"path": client_id, "outcome": "no_credentials"}
        base, realm = self.admin.base, self.admin.realm
        username = "sova-probe-" + uuid4().hex[:12]
        with httpx.Client(timeout=self.timeout) as client:
            token = client.post(f"{base}/realms/{realm}/protocol/openid-connect/token", data={
                "grant_type": "client_credentials", "client_id": client_id,
                "client_secret": secret})
            if token.status_code != 200:
                return False, {"path": client_id, "outcome": "no_token",
                               "status": token.status_code}
            created = client.post(f"{base}/admin/realms/{realm}/users",
                                  headers={"Authorization": "Bearer "
                                           + token.json()["access_token"]},
                                  json={"username": username, "enabled": False})
        if created.status_code == 201:
            self._remove(username)
            return True, {"path": client_id, "outcome": "created_and_removed"}
        if created.status_code in (401, 403):
            return False, {"path": client_id, "outcome": "refused",
                           "status": created.status_code}
        return None, {"path": client_id, "outcome": "unexpected",
                      "status": created.status_code}

    def denied(self, *, registration=True):
        """``(all_denied, observation)`` over every writer path."""
        results = {}
        if registration:
            results["self_registration"] = self.can_register()
        for client_id in sorted(self.writer_secrets):
            results[client_id] = self.can_manage_users(client_id)
        return all(r is False for r, _ in results.values()) and bool(results), {
            "probe": "account_writers_denied",
            "paths": {path: detail for path, (_, detail) in results.items()}}
