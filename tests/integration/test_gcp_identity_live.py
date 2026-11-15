"""Google Cloud Identity Platform (the cloud identity provider) to Keycloak, measured.

A run-scoped tenant in the real project holds synthetic users with real
passwords and groups (a ``groups`` custom claim). Accounts move to a local
Keycloak realm without their credentials; the probes sign in at both sides
for real and verify the ID tokens against each issuer's published keys
(Identity Platform's own included). No e-mail is sent.
"""

from datetime import UTC, datetime, timedelta
import json

from aws_fixtures import reset_password
from gcp_fixtures import idp_login
from keycloak_fixtures import CLIENT
import pytest

from sova.adapters.keycloak_identity import SUBJECT, KeycloakIdentityMigration
from sova.adapters.oidc_probes import OidcProbes
from sova.domain.contracts import EffectCommand, EffectStatus, ResourceScope

pytestmark = [pytest.mark.gcp_live, pytest.mark.keycloak]


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-identity", "app-identity", "keycloak-target", "unit-identity"),
        "operation-identity", "migrate", adapter.adapter_id, "migrate_identities",
        "operation-identity:migrate", "d" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), json.dumps({"identity": True}))


def migration(idp_tenant, keycloak_target):
    return KeycloakIdentityMigration("identity-platform-to-keycloak",
                                     source=idp_tenant["directory"],
                                     target=keycloak_target["admin"], mfa_required=False)


def migrate(idp_tenant, keycloak_target):
    adapter = migration(idp_tenant, keycloak_target)
    adapter.apply(command(adapter))
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    return observation.status, observation.evidence


def test_accounts_move_to_keycloak_with_groups_but_without_passwords(idp_tenant,
                                                                     keycloak_target):
    status, evidence = migrate(idp_tenant, keycloak_target)
    assert status == EffectStatus.APPLIED, evidence
    assert evidence["source_accounts"] == 3 and evidence["federated_accounts"] == []
    tokens, error = idp_login(idp_tenant, "alice@example.test", idp_tenant["passwords"]["alice"])
    assert error is None, error
    source = OidcProbes(client_id=idp_tenant["project"])
    claims = source.verify(idp_tenant["directory"].issuer, tokens["idToken"])
    assert claims["sub"] == idp_tenant["uids"]["alice"]
    assert claims["firebase"]["tenant"] == idp_tenant["tenant"]
    alice = keycloak_target["server"].find(keycloak_target["realm"], "alice@example.test")
    assert alice["attributes"][SUBJECT] == [claims["sub"]]  # (issuer, sub) kept
    assert keycloak_target["server"].find(keycloak_target["realm"], "carol@example.test") is None

    target = OidcProbes(client_id=CLIENT)
    passed, observation = target.fresh_login(keycloak_target["issuer"], "alice@example.test",
                                             idp_tenant["passwords"]["alice"])
    assert not passed and observation["outcome"] == "invalid_grant"  # no password moved
    reset_password(keycloak_target, "alice@example.test", "Alice-target-pw-2!")
    moved, error = target.login(keycloak_target["issuer"], "alice@example.test",
                                "Alice-target-pw-2!")
    assert error is None
    target_claims = target.verify(keycloak_target["issuer"], moved["id_token"])
    assert sorted(claims["groups"]) == sorted(target_claims["groups"])
    # An Identity Platform refresh token means nothing to the new issuer.
    refreshed, error = target.refresh(keycloak_target["issuer"], tokens["refreshToken"])
    assert refreshed is None and error not in (None, "unreachable")


def test_sign_in_continues_with_identity_platform_blocked(idp_tenant, keycloak_target):
    """IAM-08: the source refuses every password sign-in (the tenant's password
    provider is off); migrated users still sign in at the target."""
    assert migrate(idp_tenant, keycloak_target)[0] == EffectStatus.APPLIED
    reset_password(keycloak_target, "bob@example.test", "Bob-target-pw-2!")
    idp_tenant["rest"].patch(idp_tenant["tenant_url"], {"allowPasswordSignup": False},
                             params={"updateMask": "allowPasswordSignup"})
    blocked, error = idp_login(idp_tenant, "bob@example.test", idp_tenant["passwords"]["bob"])
    assert blocked is None and error, error
    passed, observation = OidcProbes(client_id=CLIENT).fresh_login(
        keycloak_target["issuer"], "bob@example.test", "Bob-target-pw-2!")
    assert passed, observation


def test_a_change_at_the_source_after_migration_reads_back_unknown(idp_tenant, keycloak_target):
    adapter = migration(idp_tenant, keycloak_target)
    adapter.apply(command(adapter))
    idp_tenant["rest"].post(idp_tenant["accounts"] + ":update", {
        "localId": idp_tenant["uids"]["bob"], "disableUser": True})
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["mismatched"] == ["bob@example.test"]
    assert migrate(idp_tenant, keycloak_target)[0] == EffectStatus.APPLIED
    assert keycloak_target["server"].find(keycloak_target["realm"],
                                          "bob@example.test")["enabled"] is False
