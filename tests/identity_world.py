"""A Keycloak account set as a handoff unit (ADR-0002), with real writer paths:
the realm's self-registration form (the application) and a back-office
client whose service account may manage users (another registered writer).
Shared by the conformance kit and the two-unit control-plane tests."""

import hashlib
import json
from uuid import uuid4

from sova.adapters.keycloak_identity import KeycloakAdmin
from sova.adapters.keycloak_identity_handoff import KeycloakIdentityHandoff
from sova.adapters.keycloak_writer_probes import KeycloakWriterProbes

BACKOFFICE, BACKOFFICE_SECRET = "backoffice", "backoffice-test-secret"


class Route:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class IdentityWorld:
    def __init__(self, servers, realm):
        self.s, self.realm = servers, realm
        self.issuers = {side: servers[side].realm(realm) for side in ("source", "target")}
        source = servers["source"]
        source.call("PUT", f"/{realm}", {"registrationAllowed": True})
        for side in ("source", "target"):  # the back office exists on both sides
            servers[side].call("POST", f"/{realm}/clients", {
                "clientId": BACKOFFICE, "publicClient": False, "serviceAccountsEnabled": True,
                "standardFlowEnabled": False, "secret": BACKOFFICE_SECRET})
        self._grant_backoffice("source")
        for name, groups in (("alice", ("finance",)), ("bob", ("finance", "support")),
                             ("dave", ())):
            source.user(realm, name, name + "-pw-1", groups=groups)
        source.user(realm, "carol", "carol-pw-1", enabled=False)
        self._route = Route(self.issuers["source"])
        self.pending = None

    # -- plumbing --------------------------------------------------------------
    def admin(self, side):
        user, password = self.s["admin"]
        return KeycloakAdmin(self.s[side].url, self.realm, username=user, password=password)

    def _call(self, side, method, path, body=None, **params):
        return self.s[side].call(method, f"/{self.realm}{path}", body, **params)

    def _backoffice_account(self, side):
        client = self._call(side, "GET", "/clients", clientId=BACKOFFICE)[0]["id"]
        return self._call(side, "GET", f"/clients/{client}/service-account-user")["id"]

    def _grant_backoffice(self, side):
        management = self._call(side, "GET", "/clients", clientId="realm-management")[0]["id"]
        role = self._call(side, "GET", f"/clients/{management}/roles/manage-users")
        self._call(side, "POST", f"/users/{self._backoffice_account(side)}/role-mappings/"
                   f"clients/{management}", [role])

    def _handoff(self, source, target, adapter_id="keycloak-identity-handoff"):
        return KeycloakIdentityHandoff(
            adapter_id, source=self.admin(source), target=self.admin(target),
            route=self._route, mfa_required=False, writer_clients=(BACKOFFICE,))

    # -- the world contract of the kit -----------------------------------------
    def adapter(self):
        return self._handoff("source", "target")

    def reverse_adapter(self):
        return self._handoff("target", "source")

    def probes(self, side):
        return KeycloakWriterProbes(self.admin(side),
                                    writer_secrets={BACKOFFICE: BACKOFFICE_SECRET})

    def can_write(self, side, writer):
        probes = self.probes(side)
        result, _ = (probes.can_register() if writer == "app"
                     else probes.can_manage_users(BACKOFFICE))
        assert result is not None, f"{writer} probe on {side} was not decided"
        return result

    def _users(self, side):
        return self._call(side, "GET", "/users", briefRepresentation="false", max=1000)

    def business_write(self, count):
        for _ in range(count):
            name = "user-" + uuid4().hex[:8]
            self.s["source"].user(self.realm, name, name + "-pw-1", groups=("support",))

    def business_delete(self, count):
        enabled = sorted((u for u in self._users("source") if u["enabled"]),
                         key=lambda u: u["username"])
        for user in enabled[:count]:
            self._call("source", "DELETE", f"/users/{user['id']}")

    def content(self, side):
        rows = []
        for user in self._users(side):
            if not user["enabled"]:
                continue
            groups = sorted(g["path"] for g in self._call(side, "GET",
                                                           f"/users/{user['id']}/groups"))
            rows.append([user["username"], user.get("email"),
                         user.get("emailVerified", False), groups])
        return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()

    def _access(self, side):
        realm = self._call(side, "GET", "")
        profile = self._call(side, "GET", "/users/profile")
        edit = sorted((a["name"], tuple(a.get("permissions", {}).get("edit", ())))
                      for a in profile["attributes"])
        management = self._call(side, "GET", "/clients", clientId="realm-management")[0]["id"]
        roles = sorted(r["name"] for r in self._call(
            side, "GET", f"/users/{self._backoffice_account(side)}/role-mappings/clients/"
            f"{management}/composite"))
        return (realm["enabled"], realm.get("registrationAllowed"),
                realm.get("editUsernameAllowed"), tuple(edit), tuple(roles))

    def state(self):
        pending = sorted((u["username"], tuple(u.get("requiredActions") or ()))
                         for u in self._users("source") if u.get("requiredActions"))
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), tuple(pending), self._route.get())

    def reopen_source(self):
        """The source realm restored from an export taken before the handoff."""
        self._call("source", "PUT", "", {"registrationAllowed": True})
        profile = self._call("source", "GET", "/users/profile")
        for attribute in profile["attributes"]:
            if attribute["name"] in ("username", "email", "firstName", "lastName"):
                attribute["permissions"]["edit"] = ["admin", "user"]
        self._call("source", "PUT", "/users/profile", profile)
        self._grant_backoffice("source")

    def tamper_target(self):
        user = next(u for u in self._users("target") if u["username"] == "alice")
        self._call("target", "PUT", f"/users/{user['id']}", {**user,
                                                              "email": "x@example.invalid"})

    def move_route(self, side):
        self._route.set(self.issuers[side])

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self.issuers[side]

    def open_pending_write(self):
        user = next(u for u in self._users("source") if u["username"] == "bob")
        self.pending = user["id"]
        self._call("source", "PUT", f"/users/{user['id']}",
                   {**user, "requiredActions": ["VERIFY_EMAIL"]})

    def resolve_pending_write(self):
        user = self._call("source", "GET", f"/users/{self.pending}")
        self._call("source", "PUT", f"/users/{self.pending}", {**user, "requiredActions": []})
