"""Reference identity migration between two Keycloak realms (Part II, RESET_AND_REENROLL).

``KeycloakIdentityMigration`` moves accounts from a source realm to a target
realm on another server: profile, enabled and email-verified state, top-level
group membership, and the stable source subject. It never moves credentials:
passwords and MFA secrets are not portable between identity providers, so
every migrated account must set a new password (and enrol MFA again when the
contract requires MFA) before it can sign in at the target.

The source subject is kept in a target attribute only administrators may view
or edit. A user able to edit it could claim someone else's source identity, so
the readback refuses a target whose user profile lets users touch it.

The readback reads both realms again and holds only if

* every enabled source account maps to exactly one target account and no two
  target accounts claim the same source subject (IAM-01);
* username, email, enabled and email-verified state and groups match (IAM-06,
  and the group part of IAM-04);
* target accounts whose source account is gone are disabled, not left active;
* no target account with the same username exists unmapped. Such an account
  is never merged automatically (that would hand an existing account to
  whoever owned the source one); the operator resolves it.

Only top-level groups are supported; a nested group membership keeps the
readback unconfirmed. ``max_users`` bounds every listing.
"""

import httpx

from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"migrate_identities"})
SUBJECT = "sova_source_subject"
_PAGE = 100


class KeycloakAdmin:
    """Administrative REST access to one realm on one Keycloak server."""

    def __init__(self, base_url, realm, *, username, password, timeout=10.0):
        self.base, self.realm = base_url.rstrip("/"), realm
        self.username, self.password, self.timeout = username, password, timeout

    @property
    def issuer(self):
        return f"{self.base}/realms/{self.realm}"

    def _token(self, client):
        response = client.post(f"{self.base}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": self.username, "password": self.password})
        response.raise_for_status()
        return response.json()["access_token"]

    def session(self):
        client = httpx.Client(timeout=self.timeout)
        client.headers["Authorization"] = "Bearer " + self._token(client)
        return _Realm(client, f"{self.base}/admin/realms/{self.realm}")


class _Realm:
    def __init__(self, client, url):
        self.client, self.url = client, url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def get(self, path, **params):
        response = self.client.get(self.url + path, params=params)
        response.raise_for_status()
        return response.json()

    def send(self, method, path, body=None):
        response = self.client.request(method, self.url + path, json=body)
        response.raise_for_status()
        return response

    def users(self, bound):
        users, first = [], 0
        while True:
            page = self.get("/users", first=first, max=_PAGE, briefRepresentation="false")
            users += page
            if len(users) > bound:
                raise _TooMany
            if len(page) < _PAGE:
                return users
            first += _PAGE

    def groups_of(self, user_id):
        return sorted(g["path"] for g in self.get(f"/users/{user_id}/groups"))

    def top_groups(self):
        return {g["path"]: g["id"] for g in self.get("/groups", briefRepresentation="true",
                                                       max=1000)}

    def profile(self):
        return self.get("/users/profile")


class _TooMany(ContractError):
    """More accounts than this adapter is bounded to read."""


def _subject(user):
    values = (user.get("attributes") or {}).get(SUBJECT) or []
    return values[0] if len(values) == 1 else None


class KeycloakIdentityMigration:
    def __init__(self, adapter_id, *, source, target, mfa_required, max_users=10_000):
        if not isinstance(source, KeycloakAdmin) or not isinstance(target, KeycloakAdmin):
            raise ContractError("Source and target realm administration required")
        if type(mfa_required) is not bool:
            raise ContractError("Explicit MFA requirement required")
        if type(max_users) is not int or not 1 <= max_users <= 1_000_000:
            raise ContractError("Bounded account count required")
        self.adapter_id, self.source, self.target = adapter_id, source, target
        self.required_actions = ["UPDATE_PASSWORD"] + (["CONFIGURE_TOTP"] if mfa_required
                                                       else [])
        self.max_users = max_users

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported identity action for this adapter")

    def apply(self, command):
        self._migrate()

    def readback(self, command, now):
        try:
            holds, detail = self._check()
        except _TooMany:
            holds, detail = False, {"reason": "account bound exceeded",
                                    "max_users": self.max_users}
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))

    # -- reading ---------------------------------------------------------------
    def _wanted(self, user, groups):
        return {"username": user["username"], "email": user.get("email"),
                "enabled": user["enabled"], "emailVerified": user.get("emailVerified", False),
                "groups": groups}

    def _snapshot(self):
        with self.source.session() as src, self.target.session() as tgt:
            source = {u["id"]: self._wanted(u, src.groups_of(u["id"]))
                      for u in src.users(self.max_users)}
            target_users = tgt.users(self.max_users)
            target = {u["id"]: {**self._wanted(u, tgt.groups_of(u["id"])),
                                "subject": _subject(u)} for u in target_users}
            profile = tgt.profile()
        return source, target, profile

    def _protected(self, profile):
        for attribute in profile.get("attributes", ()):
            if attribute.get("name") == SUBJECT:
                permissions = attribute.get("permissions") or {}
                return (permissions.get("edit") == ["admin"]
                        and permissions.get("view") == ["admin"]
                        and not attribute.get("multivalued", False))
        return False

    def _check(self):
        source, target, profile = self._snapshot()
        claims = {}
        for tid, t in target.items():
            if t["subject"] is not None:
                claims.setdefault(t["subject"], []).append(tid)
        duplicates = sorted(s for s, ids in claims.items() if len(ids) > 1)
        unmapped, mismatched, nested = [], [], []
        for sid, s in source.items():
            if any(g.count("/") > 1 for g in s["groups"]):
                nested.append(s["username"])
            ids = claims.get(sid, [])
            if not ids:
                if s["enabled"]:
                    unmapped.append(s["username"])
                continue
            t = {k: v for k, v in target[ids[0]].items() if k != "subject"}
            if t != s:
                mismatched.append(s["username"])
        orphans = sorted(t["username"] for t in target.values()
                         if t["subject"] is not None and t["subject"] not in source
                         and t["enabled"])
        mapped_names = {target[ids[0]]["username"] for ids in claims.values()}
        conflicts = sorted(t["username"] for t in target.values()
                           if t["subject"] is None and t["username"] in
                           {s["username"] for s in source.values()}
                           and t["username"] not in mapped_names)
        protected = self._protected(profile)
        holds = protected and not any((duplicates, unmapped, mismatched, nested, orphans,
                                       conflicts))
        return holds, {
            "source_accounts": len(source), "mapped": len(claims),
            "subject_attribute_admin_only": protected,
            "duplicate_subjects": len(duplicates), "unmapped": unmapped[:20],
            "mismatched": mismatched[:20], "nested_groups": nested[:20],
            "active_orphans": orphans[:20], "username_conflicts": conflicts[:20]}

    # -- migration -------------------------------------------------------------
    def _ensure_protected_attribute(self, tgt):
        profile = tgt.profile()
        attributes = [a for a in profile.get("attributes", []) if a.get("name") != SUBJECT]
        attributes.append({"name": SUBJECT, "displayName": "Source subject",
                           "multivalued": False,
                           "permissions": {"view": ["admin"], "edit": ["admin"]}})
        tgt.send("PUT", "/users/profile", {**profile, "attributes": attributes})

    def _migrate(self):
        with self.source.session() as src, self.target.session() as tgt:
            self._ensure_protected_attribute(tgt)
            source = src.users(self.max_users)
            target = tgt.users(self.max_users)
            by_subject = {_subject(u): u for u in target if _subject(u)}
            by_name = {u["username"]: u for u in target}
            groups = tgt.top_groups()
            for user in source:
                wanted_groups = src.groups_of(user["id"])
                if any(g.count("/") > 1 for g in wanted_groups):
                    continue  # nested groups are reported by the readback
                existing = by_subject.get(user["id"])
                if existing is None and user["username"] in by_name:
                    continue  # an unmapped account with this name: never merged
                if existing is None and not user["enabled"]:
                    continue  # a disabled account is not created at the target
                body = {"username": user["username"], "email": user.get("email"),
                        "firstName": user.get("firstName"), "lastName": user.get("lastName"),
                        "enabled": user["enabled"],
                        "emailVerified": user.get("emailVerified", False),
                        "attributes": {SUBJECT: [user["id"]]}}
                if existing is None:
                    body["requiredActions"] = list(self.required_actions)
                    location = tgt.send("POST", "/users", body).headers["Location"]
                    target_id = location.rstrip("/").rsplit("/", 1)[1]
                else:
                    target_id = existing["id"]
                    attributes = {**(existing.get("attributes") or {}), SUBJECT: [user["id"]]}
                    tgt.send("PUT", f"/users/{target_id}", {**body, "attributes": attributes})
                current = set(tgt.groups_of(target_id))
                for path in sorted(set(wanted_groups) - current):
                    if path not in groups:
                        tgt.send("POST", "/groups", {"name": path.lstrip("/")})
                        groups = tgt.top_groups()
                    tgt.send("PUT", f"/users/{target_id}/groups/{groups[path]}")
                for path in sorted(current - set(wanted_groups)):
                    tgt.send("DELETE", f"/users/{target_id}/groups/{groups[path]}")
            sources = {u["id"] for u in source}
            for user in target:
                if _subject(user) and _subject(user) not in sources and user["enabled"]:
                    tgt.send("PUT", f"/users/{user['id']}", {**user, "enabled": False})
