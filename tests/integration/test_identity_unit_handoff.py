"""Two units of one migration through the real control plane (ADR-0002).

The accounts are their own handoff unit: compiled from the same contract and
snapshot, approved, admitted and run with the same authority protocol, effect
ledger and signed evidence as the data. The data unit's activation intent
requires the account unit's completion, read from its authority record:
before it, the data unit is held with the target never activated; after it,
the data unit completes and the application signs users in at the target.
"""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
import time
from uuid import uuid4

from handoff_drive import drive, pieces, without_transport_control
from handoff_support import admitted_handoff
from identity_world import BACKOFFICE, IdentityWorld
from keycloak_fixtures import CLIENT
from postgres_business import Route
import pytest

from sova.adapters.identity_probes import IdentityMappingProbes
from sova.adapters.keycloak_identity import KeycloakDirectory
from sova.adapters.oidc_probes import OidcProbes
from sova.application.unit_ordering import UnitCompletion
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.postgres, pytest.mark.keycloak]
IDP = "sign-in"
DATA = ("api-a", "api-b", "database")
ZONE = "approved-zone-a"
IDENTITY = {
    "principal_namespace": "organization-scoped-stable-id",
    "stable_subject_mapping_required": True,
    "allowed_external_idps": ["target-keycloak"],
    "remove_dependencies": ["source-keycloak"],
    "allow_dual_issuer_during_transition": False,
    "dual_issuer_max_duration": None,
    "password_transition_policy": "reset",
    "mfa": {"required": False, "allowed_methods": [], "reenrollment_allowed": False,
            "user_communication_approved": True},
    "sessions": {"forced_reauthentication_allowed": True,
                 "refresh_token_portability_assumed": False},
    "authorization": {"deny_cross_tenant_access": True,
                      "source_target_permission_equivalence_required": True},
    "evidence": {"fresh_login_required": True, "fresh_token_refresh_required": True,
                 "denied_access_test_required": True,
                 "source_idp_blocked_cold_start_required": True},
}


def adjust(data):
    without_transport_control(data)
    data["spec"]["identity"] = json.loads(json.dumps(IDENTITY))
    data["spec"]["exit"] = {"remove": ["source-keycloak", "source-db"],
                            "allowed_remaining": ["target-keycloak", "customer-owned-idp"]}


class Accounts:
    """The account world plus what the identity obligations measure."""

    def __init__(self, servers):
        self.s = servers
        self.realm = "accounts-" + uuid4().hex[:10]
        self.world = IdentityWorld(servers, self.realm)
        # The environment owner's target settings: zone label, audit and
        # lock-out and recovery paths (IAM-09).
        servers["target"].call("PUT", f"/{self.realm}", {
            "attributes": {"zone": ZONE}, "eventsEnabled": True,
            "bruteForceProtected": True, "resetPasswordAllowed": True})
        self.outsider = "outsider-" + uuid4().hex[:8]  # another tenant's realm
        servers["target"].realm(self.outsider)
        servers["target"].user(self.outsider, "mallory", "Mallory-pw-1!")
        self.oidc = OidcProbes(client_id=CLIENT)
        self.passwords = {}
        self.migrated_at = None

    def close(self):
        for side, realm in (("source", self.realm), ("target", self.realm),
                            ("target", self.outsider)):
            self.s[side].call("DELETE", f"/{realm}")

    @property
    def issuer(self):
        return self.world.issuers["target"]

    def reset(self, username):
        """What each user does at the target's reset page, done here directly."""
        password = username.title() + "-target-" + secrets.token_urlsafe(8) + "!1"
        user = self.s["target"].find(self.realm, username)
        self.s["target"].call("PUT", f"/{self.realm}/users/{user['id']}/reset-password",
                              {"type": "password", "value": password, "temporary": False})
        self.s["target"].call("PUT", f"/{self.realm}/users/{user['id']}",
                              {**user, "requiredActions": []})
        self.passwords[username] = password

    # -- measurements ------------------------------------------------------------
    def zone(self, obligation):
        realm = self.s["target"].call("GET", f"/{self.realm}")
        label = (realm.get("attributes") or {}).get("zone")
        return label in obligation["allowed_zones"], {"probe": "zone", "label": label}

    def writers_denied(self, _):
        return self.world.probes("source").denied()

    def single_writer(self, _):
        target = self.world.probes("target")
        registered, _ = target.can_register()
        backoffice, _ = target.can_manage_users(BACKOFFICE)
        source, observation = self.world.probes("source").denied()
        return registered is True and backoffice is False and source, {
            "probe": "single_writer", "target_registration": registered,
            "target_backoffice": backoffice, "source": observation["paths"]}

    def cross_tenant(self, _):
        """Another tenant's token is refused by this realm's admin API."""
        tokens, error = self.oidc.login(self.world.s["target"].url + f"/realms/{self.outsider}",
                                        "mallory", "Mallory-pw-1!")
        if tokens is None:
            return False, {"probe": "cross_tenant_admin_denied", "outcome": error}
        import httpx

        status = httpx.get(f"{self.s['target'].url}/admin/realms/{self.realm}/users",
                           headers={"Authorization": "Bearer " + tokens["access_token"]},
                           timeout=10).status_code
        return status in (401, 403), {"probe": "cross_tenant_admin_denied", "status": status}

    def _accounts(self, realm):
        rows = []
        for user in self.s["target"].call("GET", f"/{realm}/users", briefRepresentation="false",
                                          max=1000):
            if user["enabled"]:
                groups = sorted(g["path"] for g in self.s["target"].call(
                    "GET", f"/{realm}/users/{user['id']}/groups"))
                rows.append([user["username"], user.get("email"),
                             user.get("emailVerified", False), groups,
                             (user.get("attributes") or {}).get("sova_source_subject")])
        return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(), len(rows)

    def restore_drill(self, _):
        """Rebuild the target's accounts in an empty realm and compare."""
        scratch = "restore-" + uuid4().hex[:8]
        server = self.s["target"]
        server.call("POST", "", {"realm": scratch, "enabled": False})
        try:
            # The same user profile, so the source-subject attribute is kept.
            server.call("PUT", f"/{scratch}/users/profile",
                        server.call("GET", f"/{self.realm}/users/profile"))
            for group in server.call("GET", f"/{self.realm}/groups"):
                server.call("POST", f"/{scratch}/groups", {"name": group["name"]})
            for user in server.call("GET", f"/{self.realm}/users", briefRepresentation="false",
                                    max=1000):
                if not user["enabled"]:
                    continue
                groups = [g["path"] for g in server.call(
                    "GET", f"/{self.realm}/users/{user['id']}/groups")]
                server.call("POST", f"/{scratch}/users", {
                    **{k: user[k] for k in ("username", "email", "emailVerified", "enabled",
                                            "firstName", "lastName", "attributes")
                       if k in user},
                    "groups": groups})
            original, restored = self._accounts(self.realm), self._accounts(scratch)
            return original == restored and original[1] > 0, {
                "probe": "restore_drill", "accounts": original[1],
                "equal": original == restored}
        finally:
            server.call("DELETE", f"/{scratch}")

    def mapping(self):
        return IdentityMappingProbes(source=KeycloakDirectory(self.world.admin("source")),
                                     target=self.world.admin("target"))

    def no_credential_copied(self, _):
        found = []
        for user in self.s["target"].call("GET", f"/{self.realm}/users", max=1000):
            credentials = self.s["target"].call(
                "GET", f"/{self.realm}/users/{user['id']}/credentials")
            found += [user["username"] for c in credentials
                      if self.migrated_at is None or c.get("createdDate", 0) < self.migrated_at]
        return not found, {"probe": "no_credential_copied", "users_with_credentials": found}

    def audit_lock_recovery(self, _):
        realm = self.s["target"].call("GET", f"/{self.realm}")
        paths = {k: realm.get(k) is True for k in ("eventsEnabled", "bruteForceProtected",
                                                   "resetPasswordAllowed")}
        return all(paths.values()), {"probe": "audit_lock_recovery_paths", **paths}

    def permission_equivalence(self, _):
        return self.oidc.permission_equivalence(
            self.world.issuers["source"], self.issuer, "bob", "bob-pw-1",
            self.passwords.get("bob", "unset"))

    def fresh_login(self, _):
        return self.oidc.fresh_login(self.issuer, "alice", self.passwords.get("alice", "unset"))

    def fresh_refresh(self, _):
        return self.oidc.fresh_refresh(self.issuer, "alice",
                                       self.passwords.get("alice", "unset"))

    def login_slo(self, obligation, budget_ms):
        started = time.monotonic()
        passed, observation = self.fresh_login(obligation)
        spent = (time.monotonic() - started) * 1000
        return passed and spent <= budget_ms, {"probe": "login_slo", "spent_ms": round(spent),
                                               "budget_ms": budget_ms, "login": observation}

    def identity_measures(self, budget_ms):
        mapping = self.mapping()
        return {
            "IAM-01.subject_mapping_unique": lambda _: mapping.subject_mapping_unique(),
            "IAM-06.account_state_parity": lambda _: mapping.account_state_parity(),
            "IAM-07.no_secret_or_pii_leak": self.no_credential_copied,
            "IAM-02.fresh_login_and_validation": self.fresh_login,
            "IAM-02.fresh_token_refresh": self.fresh_refresh,
            "IAM-04.cross_tenant_denial": lambda _: self.oidc.cross_tenant_denied(
                self.issuer, "mallory", "Mallory-pw-1!"),
            "IAM-04.permission_equivalence": self.permission_equivalence,
            "IAM-09.audit_lock_recovery_paths": self.audit_lock_recovery,
            "IAM-12.login_slo": lambda o: self.login_slo(o, budget_ms),
        }

    def unit_measures(self, plan_doc, budget_ms):
        measures = {"fence.source_writes_denied": self.writers_denied,
                    "route.stale_writes_rejected": self.writers_denied,
                    "authority.single_writer_after_activation": self.single_writer,
                    "security.deny_cross_tenant_data_access": self.cross_tenant,
                    "security.tested_restore": self.restore_drill,
                    "operations.restore_test": self.restore_drill,
                    **self.identity_measures(budget_ms)}
        for o in plan_doc["obligations"]:
            if o["kind"] == "placement_evidence":
                measures[o["id"]] = self.zone
        return measures


@pytest.fixture
def accounts(keycloak_servers):
    found = Accounts(keycloak_servers)
    yield found
    found.close()


def units(postgres_database):
    now = datetime.now(UTC) - timedelta(seconds=1)
    account_unit = admitted_handoff(postgres_database, now, adjust=adjust, nodes=(IDP,),
                                    identity_provider=IDP, approval_id="approval-accounts")
    data_unit = admitted_handoff(postgres_database, now, nodes=DATA, identity_provider=IDP,
                                 alongside=account_unit, approval_id="approval-data")
    return account_unit, data_unit


def run_accounts(h, accounts, until=None):
    doc = json.loads(h["revision"].canonical)
    budget = h["world"]["contract"].document["spec"]["availability"][
        "max_client_completion_wait_ms"]
    adapter = accounts.world._handoff("source", "target",
                                      adapter_id=h["world"]["manifest"].adapter_id)

    def after(op):
        if op["kind"] in ("copy_content", "apply_final_changes"):
            accounts.migrated_at = accounts.migrated_at or int(time.time() * 1000) - 1000
        if op["kind"] == "commit_route":
            for user in ("alice", "bob"):  # users reset their password at the target
                accounts.reset(user)

    record, observed = drive(h, h["revision"], h["plan"], adapter=adapter, probes=None,
                             other_db=None, scratch_db=None, operation="accounts-handoff",
                             approval_id=h["approval_id"], after_effect=after, until=until,
                             measures=accounts.unit_measures(doc, budget))
    return record, observed


def run_data(h, business, accounts, route):
    budget = h["world"]["contract"].document["spec"]["availability"][
        "max_client_completion_wait_ms"]
    adapter, probes = pieces(business, h["world"]["manifest"].adapter_id, source="src",
                             target="tgt", route=route)
    measures = {**accounts.identity_measures(budget),
                "identity.unit_handoff_complete": lambda o: UnitCompletion(h["store"]).check(
                    h["app"], o)}
    names = business["names"]
    record, observed = drive(h, h["revision"], h["plan"], adapter=adapter, probes=probes,
                             other_db=names["other"], scratch_db=names["scratch"],
                             operation="data-handoff", approval_id=h["approval_id"],
                             measures=measures)
    return record, observed


def test_the_data_unit_is_held_until_the_account_unit_completes(postgres_database, business,
                                                               accounts):
    """The accounts were copied and verified, but their unit stopped before its
    own activation: the data unit's mapping checks pass, yet its activation
    intent is refused and the target database never takes writes."""
    account_unit, data_unit = units(postgres_database)
    record, _ = run_accounts(account_unit, accounts, until="record_activation_intent")
    assert record.state == HandoffState.FINAL_STATE_VERIFIED
    route = Route(business["names"]["src"])
    record, observed = run_data(data_unit, business, accounts, route)
    assert record.state == HandoffState.SAFE_HOLD and record.reason == "CONTRACT_BREACH"
    assert not record.activation_possible  # held before the target could take writes
    failed = dict(observed["failed"])
    assert failed["identity.unit_handoff_complete"]["state"] == "FINAL_STATE_VERIFIED"
    assert "IAM-01.subject_mapping_unique" not in failed  # the accounts are there
    assert route.get() == business["names"]["src"]


def test_accounts_then_data_complete_through_one_protocol(postgres_database, business,
                                                          accounts):
    account_unit, data_unit = units(postgres_database)
    record, observed = run_accounts(account_unit, accounts)
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, record.held_from, observed.get("uncertain"), observed.get("failed"))
    assert accounts.world.route() == accounts.issuer  # the application's issuer moved
    assert accounts.world.probes("source").denied()[0]  # the old IdP takes no account writes

    route = Route(business["names"]["src"])
    record, observed = run_data(data_unit, business, accounts, route)
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, record.held_from, observed.get("uncertain"), observed.get("failed"))
    assert route.get() == business["names"]["tgt"]
    # Both units are complete in one authority store, each its own record.
    store, app = data_unit["store"], data_unit["app"]
    units_done = {doc["unit"]["unit_digest"]: store.current(app, doc["unit"]["unit_digest"])
                  for doc in (json.loads(account_unit["revision"].canonical),
                              json.loads(data_unit["revision"].canonical))}
    assert len(units_done) == 2
    assert all(r.state == HandoffState.HANDOFF_COMPLETE for r in units_done.values())
