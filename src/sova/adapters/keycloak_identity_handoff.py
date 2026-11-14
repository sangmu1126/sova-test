"""An account set as its own handoff unit: Keycloak realm to Keycloak realm (ADR-0002).

The same template steps, authority protocol and effect ledger as a data unit;
only the systems differ. The writers of an account set are the paths that
create or change accounts:

* the application's path: self-registration, and users editing their own
  profile (username, email, first and last name);
* other registered writers: back-office clients whose service accounts may
  manage users (``writer_clients``).

Steps and the postcondition each readback checks on the servers:

* stage_target - the target realm refuses sign-in (disabled), refuses
  registration, lets only administrators edit profiles or the protected
  source-subject attribute, and no writer client's service account may manage
  users there;
* start_sync - the accounts were copied once while the source still serves
  (a sync mark on the target realm records it);
* pause_admission - the source refuses registration;
* fence_source_writes - the source refuses registration and self-service
  profile edits, no writer client may manage users, no identity provider or
  user federation can create accounts, and no enabled account has a pending
  required action that would change its profile or email-verified state when
  completed (that write path outlives the fence; the operator resolves it,
  the adapter never cancels a user's action). The settings in force before
  the fence are kept on the source realm so an abort can restore them
  (``restore_source``);
* copy_content / apply_final_changes - the accounts are migrated (see
  ``KeycloakIdentityMigration``); readback is its full comparison;
* verify_final_state - verification only: the same comparison;
* activate_target - the target realm accepts sign-in, registration as the
  application needs it and self-service profile edits; writer clients stay
  without user management; the source stays fenced;
* commit_route - the application's issuer setting names the target realm;
* verify_return_target (a failback's check) - the original source realm is
  still fenced.

Credentials never move: migrated accounts set a new password at the target.
"""

import json

from sova.adapters.keycloak_identity import (
    SUBJECT,
    AccountBoundExceeded,
    KeycloakAdmin,
    KeycloakIdentityMigration,
)
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
SYNC_MARK = "sova.identity.sync_started"
PREFENCE = "sova.identity.prefence"
SELF_SERVICE = ("username", "email", "firstName", "lastName")
# Required actions that, once the user completes them, change what is migrated.
PROFILE_ACTIONS = frozenset({"UPDATE_PROFILE", "VERIFY_EMAIL", "UPDATE_EMAIL"})
MANAGE_USERS = "manage-users"


class _Side:
    """Reads and settings of one realm through its administrative session."""

    def __init__(self, admin):
        self.admin = admin

    def realm(self, session):
        return session.get("")

    def set_realm(self, session, **fields):
        session.send("PUT", "", fields)

    def self_service(self, profile):
        """Profile attributes users may edit themselves."""
        return sorted(a["name"] for a in profile.get("attributes", ())
                      if a.get("name") in SELF_SERVICE
                      and "user" in ((a.get("permissions") or {}).get("edit") or ()))

    def set_self_service(self, session, editable):
        profile = session.get("/users/profile")
        for attribute in profile.get("attributes", ()):
            if attribute.get("name") in SELF_SERVICE:
                permissions = dict(attribute.get("permissions") or {})
                permissions["edit"] = ["admin", "user"] if attribute["name"] in editable \
                    else ["admin"]
                attribute["permissions"] = permissions
        session.send("PUT", "/users/profile", profile)

    def _management(self, session):
        found = session.get("/clients", clientId="realm-management")
        return found[0]["id"] if found else None

    def writer_roles(self, session, clients):
        """Effective realm-management roles of each writer client's service account
        (``None`` for a client that does not exist in this realm)."""
        management, out = self._management(session), {}
        for client_id in clients:
            found = session.get("/clients", clientId=client_id)
            if not found or not found[0].get("serviceAccountsEnabled"):
                out[client_id] = None
                continue
            account = session.get(f"/clients/{found[0]['id']}/service-account-user")
            out[client_id] = sorted(r["name"] for r in session.get(
                f"/users/{account['id']}/role-mappings/clients/{management}/composite"))
        return out

    def revoke_writers(self, session, clients):
        """Remove every direct realm-management role of the writer clients'
        service accounts; returns what was removed (for an abort)."""
        management, removed = self._management(session), {}
        for client_id in clients:
            found = session.get("/clients", clientId=client_id)
            if not found or not found[0].get("serviceAccountsEnabled"):
                continue
            account = session.get(f"/clients/{found[0]['id']}/service-account-user")
            path = f"/users/{account['id']}/role-mappings/clients/{management}"
            direct = session.get(path)
            if direct:
                session.client.request("DELETE", session.url + path,
                                       json=direct).raise_for_status()
                removed[client_id] = sorted(r["name"] for r in direct)
        return removed

    def grant_writers(self, session, granted):
        management = self._management(session)
        for client_id, roles in granted.items():
            found = session.get("/clients", clientId=client_id)
            if not found:
                continue
            account = session.get(f"/clients/{found[0]['id']}/service-account-user")
            wanted = [session.get(f"/clients/{management}/roles/{name}") for name in roles]
            session.send("POST", f"/users/{account['id']}/role-mappings/clients/{management}",
                         wanted)

    def account_creators(self, session):
        """Other ways accounts appear: brokered first logins and user federation."""
        brokers = sorted(p["alias"] for p in session.get("/identity-provider/instances")
                         if p.get("enabled"))
        federation = sorted(c.get("name", c.get("id")) for c in session.get(
            "/components", type="org.keycloak.storage.UserStorageProvider"))
        return brokers, federation

    def pending(self, session, bound):
        return sorted(u["username"] for u in session.users(bound)
                      if u.get("enabled") and PROFILE_ACTIONS & set(u.get("requiredActions")
                                                                     or ()))


class KeycloakIdentityHandoff:
    def __init__(self, adapter_id, *, source, target, route, mfa_required,
                 writer_clients=(), registration=True, max_users=10_000):
        if not isinstance(source, KeycloakAdmin) or not isinstance(target, KeycloakAdmin):
            raise ContractError("Source and target realm administration required")
        if not all(callable(getattr(route, n, None)) for n in ("get", "set")):
            raise ContractError("The application's issuer setting required")
        if type(writer_clients) is not tuple or type(registration) is not bool:
            raise ContractError("Writer clients and the registration need required")
        self.adapter_id, self.route = adapter_id, route
        self.source, self.target = _Side(source), _Side(target)
        self.writer_clients, self.registration = writer_clients, registration
        self.max_users = max_users
        self.migration = KeycloakIdentityMigration(adapter_id, source=source, target=target,
                                                   mfa_required=mfa_required,
                                                   max_users=max_users)

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported identity handoff action for this adapter")

    def apply(self, command):
        getattr(self, "_" + command.action)()

    def readback(self, command, now):
        try:
            holds, detail = getattr(self, "_check_" + command.action)()
        except AccountBoundExceeded:
            holds, detail = False, {"reason": "account bound exceeded",
                                    "max_users": self.max_users}
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))

    # -- shared readings -------------------------------------------------------
    def _locked(self, side, *, enabled):
        """``(holds, detail)``: no account writer except administrators."""
        with side.admin.session() as session:
            realm = side.realm(session)
            editable = side.self_service(session.get("/users/profile"))
            roles = side.writer_roles(session, self.writer_clients)
            brokers, federation = side.account_creators(session)
        managing = sorted(c for c, r in roles.items() if r and MANAGE_USERS in r)
        holds = (realm["enabled"] is enabled and not realm.get("registrationAllowed")
                 and not editable and not managing)
        return holds, {"realm_enabled": realm["enabled"],
                       "registration": bool(realm.get("registrationAllowed")),
                       "self_service_edit": editable, "writers_managing_users": managing,
                       "identity_providers": brokers, "user_federation": federation}

    def _fenced(self):
        holds, detail = self._locked(self.source, enabled=True)
        with self.source.admin.session() as session:
            pending = self.source.pending(session, self.max_users)
        holds = (holds and not pending and not detail["identity_providers"]
                 and not detail["user_federation"])
        return holds, {**detail, "pending_profile_actions": pending[:20]}

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        with self.target.admin.session() as session:
            self.target.set_realm(session, enabled=False, registrationAllowed=False,
                                  editUsernameAllowed=False)
            self.target.set_self_service(session, ())
            self.target.revoke_writers(session, self.writer_clients)
            self.migration._ensure_protected_attribute(session)

    def _check_stage_target(self):
        holds, detail = self._locked(self.target, enabled=False)
        with self.target.admin.session() as session:
            protected = self.migration._protected(session.profile())
        return holds and protected, {**detail, "subject_attribute_admin_only": protected}

    def _start_sync(self):
        self.migration._migrate()
        with self.target.admin.session() as session:
            realm = self.target.realm(session)
            if SYNC_MARK not in (realm.get("attributes") or {}):
                self.target.set_realm(session, attributes={SYNC_MARK: "initial-copy-done"})

    def _check_start_sync(self):
        with self.target.admin.session() as session:
            marked = SYNC_MARK in (self.target.realm(session).get("attributes") or {})
            protected = self.migration._protected(session.profile())
        return marked and protected, {"sync_marked": marked,
                                      "subject_attribute_admin_only": protected}

    def _pause_admission(self):
        with self.source.admin.session() as session:
            self._remember_source(session)
            self.source.set_realm(session, registrationAllowed=False)

    def _check_pause_admission(self):
        with self.source.admin.session() as session:
            registration = bool(self.source.realm(session).get("registrationAllowed"))
        return not registration, {"source_registration": registration}

    def _remember_source(self, session):
        """Keep the pre-fence settings once, on the source realm itself."""
        realm = self.source.realm(session)
        if (realm.get("attributes") or {}).get(PREFENCE):
            return
        before = {"registrationAllowed": bool(realm.get("registrationAllowed")),
                  "editUsernameAllowed": bool(realm.get("editUsernameAllowed")),
                  "self_service_edit": self.source.self_service(session.get("/users/profile")),
                  "writer_roles": {}}
        self.source.set_realm(session, attributes={PREFENCE: json.dumps(before)})

    def _fence_source_writes(self):
        with self.source.admin.session() as session:
            self._remember_source(session)
            self.source.set_realm(session, registrationAllowed=False, editUsernameAllowed=False)
            self.source.set_self_service(session, ())
            removed = self.source.revoke_writers(session, self.writer_clients)
            if removed:
                attributes = self.source.realm(session).get("attributes") or {}
                before = json.loads(attributes[PREFENCE])
                for client_id, roles in removed.items():
                    before["writer_roles"].setdefault(client_id, roles)
                self.source.set_realm(session, attributes={PREFENCE: json.dumps(before)})

    def _check_fence_source_writes(self):
        return self._fenced()

    def _copy_content(self):
        self.migration._migrate()

    def _check_copy_content(self):
        return self.migration._check()

    _apply_final_changes = _copy_content
    _check_apply_final_changes = _check_copy_content

    def _verify_final_state(self):
        return None  # verification only; the readback is the comparison

    def _check_verify_final_state(self):
        return self.migration._check()

    def _activate_target(self):
        with self.target.admin.session() as session:
            self.target.set_realm(session, enabled=True,
                                  registrationAllowed=self.registration)
            self.target.set_self_service(session, SELF_SERVICE)
            self.target.revoke_writers(session, self.writer_clients)

    def _check_activate_target(self):
        with self.target.admin.session() as session:
            realm = self.target.realm(session)
            editable = self.target.self_service(session.get("/users/profile"))
            roles = self.target.writer_roles(session, self.writer_clients)
            protected = self.migration._protected(session.profile())
        managing = sorted(c for c, r in roles.items() if r and MANAGE_USERS in r)
        source_fenced, source = self._fenced()
        holds = (realm["enabled"] and bool(realm.get("registrationAllowed")) is self.registration
                 and editable == sorted(SELF_SERVICE) and not managing and protected
                 and source_fenced)
        return holds, {"target_enabled": realm["enabled"],
                       "target_registration": bool(realm.get("registrationAllowed")),
                       "target_self_service_edit": editable,
                       "target_writers_managing_users": managing,
                       "subject_attribute_admin_only": protected, "source_fenced": source_fenced,
                       "source": source}

    def _commit_route(self):
        self.route.set(self.target.admin.issuer)

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.target.admin.issuer, {"route": current}

    def _verify_return_target(self):
        return None  # verification only

    def _check_verify_return_target(self):
        # In a failback adapter the "target" is the original source realm.
        return self._locked(self.target, enabled=True)

    # -- abort -----------------------------------------------------------------
    def restore_source(self):
        """Put back the source settings in force before the fence (an aborted
        handoff, before any activation). Idempotent; a no-op if never fenced."""
        with self.source.admin.session() as session:
            attributes = self.source.realm(session).get("attributes") or {}
            if not attributes.get(PREFENCE):
                return
            before = json.loads(attributes[PREFENCE])
            self.source.set_realm(session, registrationAllowed=before["registrationAllowed"],
                                  editUsernameAllowed=before["editUsernameAllowed"])
            self.source.set_self_service(session, before["self_service_edit"])
            self.source.grant_writers(session, before["writer_roles"])
            # Realm attributes cannot be removed through an update; empty means none.
            self.source.set_realm(session, attributes={PREFENCE: ""})

    def source_restored(self):
        """``(restored, detail)``: the source accepts the writers it had before."""
        with self.source.admin.session() as session:
            realm = self.source.realm(session)
            editable = self.source.self_service(session.get("/users/profile"))
            roles = self.source.writer_roles(session, self.writer_clients)
        prefence = (realm.get("attributes") or {}).get(PREFENCE)
        return not prefence, {"registration": bool(realm.get("registrationAllowed")),
                              "self_service_edit": editable, "writer_roles": roles}


__all__ = ["ACTIONS", "KeycloakIdentityHandoff", "SUBJECT"]
