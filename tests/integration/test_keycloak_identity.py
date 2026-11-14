"""Identity migration between two independent Keycloak servers (Part II).

Real accounts, real sign-ins and real signed tokens: the source and target are
separate identity providers with their own issuers and keys. Accounts move
with their groups and source subject, never with their passwords; every
identity obligation is measured by signing in and verifying the ID token.
No real identity provider is touched.
"""

import base64
from datetime import UTC, datetime, timedelta
import json

from keycloak_fixtures import CLIENT
import pytest

from sova.adapters.keycloak_identity import SUBJECT, KeycloakAdmin, KeycloakIdentityMigration
from sova.adapters.oidc_probes import OidcProbes
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope

pytestmark = pytest.mark.keycloak
PASSWORDS = {"alice": "alice-source-pw-1", "bob": "bob-source-pw-1", "carol": "carol-pw-1"}


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-identity", "app-identity", "onprem-target", "unit-identity"),
        "operation-identity", "migrate", adapter.adapter_id, "migrate_identities",
        "operation-identity:migrate", "d" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), json.dumps({"identity": True}))


class World:
    def __init__(self, servers, realm, *, mfa_required=False):
        self.s, self.realm = servers, realm
        self.source_issuer = servers["source"].realm(realm)
        self.target_issuer = servers["target"].realm(realm)
        self.source_ids = {
            "alice": servers["source"].user(realm, "alice", PASSWORDS["alice"],
                                            groups=("finance",)),
            "bob": servers["source"].user(realm, "bob", PASSWORDS["bob"],
                                          groups=("finance", "support")),
            "carol": servers["source"].user(realm, "carol", PASSWORDS["carol"],
                                            enabled=False),
        }
        self.mfa_required = mfa_required
        self.probes = OidcProbes(client_id=CLIENT)

    def admin(self, side):
        user, password = self.s["admin"]
        return KeycloakAdmin(self.s[side].url, self.realm, username=user, password=password)

    def adapter(self):
        return KeycloakIdentityMigration("keycloak-identity", source=self.admin("source"),
                                         target=self.admin("target"),
                                         mfa_required=self.mfa_required)

    def migrate(self):
        adapter = self.adapter()
        cmd = command(adapter)
        adapter.validate(cmd)
        adapter.apply(cmd)

    def status(self):
        adapter = self.adapter()
        observation = adapter.readback(command(adapter), datetime.now(UTC))
        return observation.status, observation.evidence

    def target_user(self, username):
        return self.s["target"].find(self.realm, username)

    def reset_password(self, username, password):
        """What the user does at the target's reset page, done here directly."""
        user = self.target_user(username)
        self.s["target"].call("PUT", f"/{self.realm}/users/{user['id']}/reset-password",
                              {"type": "password", "value": password, "temporary": False})
        self.s["target"].call("PUT", f"/{self.realm}/users/{user['id']}",
                              {**user, "requiredActions": []})


@pytest.fixture
def world(keycloak_servers, realm_name):
    return World(keycloak_servers, realm_name)


def test_accounts_move_with_their_groups_and_subject_but_not_their_passwords(world):
    assert world.status()[0] == EffectStatus.UNKNOWN  # nothing migrated yet
    world.migrate()
    status, evidence = world.status()
    assert status == EffectStatus.APPLIED, evidence
    alice = world.target_user("alice")
    assert alice["attributes"][SUBJECT] == [world.source_ids["alice"]]
    assert alice["requiredActions"] == ["UPDATE_PASSWORD"]
    assert world.target_user("carol") is None  # a disabled account is not created
    # The source password does not sign anyone in at the target.
    passed, observation = world.probes.fresh_login(world.target_issuer, "alice",
                                                   PASSWORDS["alice"])
    assert not passed and observation["outcome"] == "invalid_grant"
    world.reset_password("alice", "alice-target-pw-2")
    passed, observation = world.probes.fresh_login(world.target_issuer, "alice",
                                                   "alice-target-pw-2")
    assert passed, observation
    assert world.probes.fresh_refresh(world.target_issuer, "alice", "alice-target-pw-2")[0]
    passed, observation = world.probes.permission_equivalence(
        world.source_issuer, world.target_issuer, "alice", PASSWORDS["alice"],
        "alice-target-pw-2")
    assert passed and observation["target"] == ["finance"], observation


def test_migration_is_idempotent_and_readback_reads_both_realms(world):
    world.migrate()
    world.migrate()
    assert world.status()[0] == EffectStatus.APPLIED
    groups = {g["path"]: g["id"] for g in world.s["source"].call(
        "GET", f"/{world.realm}/groups")}
    world.s["source"].call(  # someone changes the source after the migration
        "DELETE", f"/{world.realm}/users/{world.source_ids['bob']}/groups/{groups['/support']}")
    status, evidence = world.status()
    assert status == EffectStatus.UNKNOWN and evidence["mismatched"] == ["bob"]
    world.migrate()
    assert world.status()[0] == EffectStatus.APPLIED


def test_mfa_contracts_require_re_enrolment(keycloak_servers, realm_name):
    world = World(keycloak_servers, realm_name, mfa_required=True)
    world.migrate()
    assert sorted(world.target_user("bob")["requiredActions"]) == [
        "CONFIGURE_TOTP", "UPDATE_PASSWORD"]


def test_users_may_never_edit_their_source_subject(world):
    world.migrate()
    profile = world.s["target"].call("GET", f"/{world.realm}/users/profile")
    for attribute in profile["attributes"]:
        if attribute["name"] == SUBJECT:
            attribute["permissions"] = {"view": ["admin", "user"], "edit": ["admin", "user"]}
    world.s["target"].call("PUT", f"/{world.realm}/users/profile", profile)
    status, evidence = world.status()
    assert status == EffectStatus.UNKNOWN
    assert evidence["subject_attribute_admin_only"] is False


def test_an_existing_target_account_is_never_merged(world):
    world.s["target"].user(world.realm, "alice", "someone-elses-password")
    world.migrate()
    status, evidence = world.status()
    assert status == EffectStatus.UNKNOWN and evidence["username_conflicts"] == ["alice"]
    assert SUBJECT not in (world.target_user("alice").get("attributes") or {})


def test_an_account_removed_at_the_source_is_disabled_at_the_target(world):
    world.migrate()
    world.s["source"].call("DELETE", f"/{world.realm}/users/{world.source_ids['bob']}")
    status, evidence = world.status()
    assert status == EffectStatus.UNKNOWN and evidence["active_orphans"] == ["bob"]
    world.migrate()
    assert world.status()[0] == EffectStatus.APPLIED
    assert world.target_user("bob")["enabled"] is False


def test_source_refresh_tokens_are_refused_by_the_target(world):
    world.migrate()
    world.reset_password("alice", "alice-target-pw-2")
    passed, observation = world.probes.refresh_not_portable(
        world.source_issuer, world.target_issuer, "alice", PASSWORDS["alice"])
    assert passed, observation


def test_another_tenants_account_cannot_sign_in(world):
    other = "tenant-b-" + world.realm
    world.s["target"].realm(other)
    world.s["target"].user(other, "mallory", "mallory-pw-1")
    passed, observation = world.probes.cross_tenant_denied(world.target_issuer, "mallory",
                                                           "mallory-pw-1")
    assert passed, observation
    # The probe is not blind: the same account signs in at its own tenant.
    other_issuer = f"{world.s['target'].url}/realms/{other}"
    assert not world.probes.cross_tenant_denied(other_issuer, "mallory", "mallory-pw-1")[0]


def test_sign_in_continues_with_the_source_provider_blocked(world):
    world.migrate()
    world.reset_password("bob", "bob-target-pw-2")
    world.s["source"].call("PUT", f"/{world.realm}", {"realm": world.realm, "enabled": False})
    passed, observation = world.probes.source_blocked_login(
        world.source_issuer, world.target_issuer, "bob", PASSWORDS["bob"], "bob-target-pw-2")
    assert passed, observation


def test_forged_or_foreign_tokens_never_verify(world):
    tokens, error = world.probes.login(world.source_issuer, "alice", PASSWORDS["alice"])
    assert error is None
    with pytest.raises(ContractError):  # a source token is not the target's
        world.probes.verify(world.target_issuer, tokens["id_token"])
    header, payload, signature = tokens["id_token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["groups"] = ["finance", "admin"]
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(ContractError):
        world.probes.verify(world.source_issuer, f"{header}.{forged}.{signature}")
    unsigned = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    with pytest.raises(ContractError):
        world.probes.verify(world.source_issuer, f"{unsigned}.{payload}.")


def test_foreign_commands_and_unbounded_reads_are_refused(world):
    adapter = world.adapter()
    with pytest.raises(ContractError):
        adapter.validate(EffectCommand(
            ResourceScope("t", "a", "e", "r"), "op", "s", "another-adapter",
            "migrate_identities", "op:s", "d" * 64, "r", 1,
            datetime.now(UTC) + timedelta(minutes=1), "{}"))
    bounded = KeycloakIdentityMigration("keycloak-identity", source=world.admin("source"),
                                        target=world.admin("target"), mfa_required=False,
                                        max_users=1)
    observation = bounded.readback(command(bounded), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["reason"] == "account bound exceeded"
