"""AWS Cognito (the cloud identity provider) to Keycloak (on premises), measured.

A run-scoped user pool in the real AWS account holds synthetic users with real
passwords and groups. Accounts move to a local Keycloak realm without their
credentials; the probes sign in at both sides for real and verify the ID
tokens against each issuer's published keys (Cognito's own JWKS included).
No e-mail is sent: users are created with message delivery suppressed.
"""

from datetime import UTC, datetime, timedelta
import json

from aws_fixtures import cognito_login, reset_password
from keycloak_fixtures import CLIENT
import pytest

from sova.adapters.keycloak_identity import SUBJECT, KeycloakIdentityMigration
from sova.adapters.oidc_probes import OidcProbes
from sova.domain.contracts import EffectCommand, EffectStatus, ResourceScope

pytestmark = [pytest.mark.aws_live, pytest.mark.keycloak]


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-identity", "app-identity", "keycloak_target-target", "unit-identity"),
        "operation-identity", "migrate", adapter.adapter_id, "migrate_identities",
        "operation-identity:migrate", "d" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), json.dumps({"identity": True}))


def migrate(cognito, keycloak_target):
    adapter = KeycloakIdentityMigration("cognito-to-keycloak", source=cognito["directory"],
                                        target=keycloak_target["admin"], mfa_required=False)
    adapter.apply(command(adapter))
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    return observation.status, observation.evidence


def test_cognito_accounts_move_to_keycloak_with_groups_but_without_passwords(cognito,
                                                                          keycloak_target):
    status, evidence = migrate(cognito, keycloak_target)
    assert status == EffectStatus.APPLIED, evidence
    assert evidence["source_accounts"] == 3 and evidence["federated_accounts"] == []
    tokens, error = cognito_login(cognito, "alice", cognito["passwords"]["alice"])
    assert error is None
    probes = OidcProbes(client_id=cognito["client"])
    source_claims = probes.verify(cognito["directory"].issuer, tokens["IdToken"])
    alice = keycloak_target["server"].find(keycloak_target["realm"], "alice")
    assert alice["attributes"][SUBJECT] == [source_claims["sub"]]  # (issuer, sub) kept
    assert keycloak_target["server"].find(keycloak_target["realm"], "carol") is None  # disabled: not created

    target = OidcProbes(client_id=CLIENT)
    passed, observation = target.fresh_login(keycloak_target["issuer"], "alice",
                                             cognito["passwords"]["alice"])
    assert not passed and observation["outcome"] == "invalid_grant"  # no password moved
    reset_password(keycloak_target, "alice", "Alice-keycloak_target-pw-2!")
    tokens_target, error = target.login(keycloak_target["issuer"], "alice", "Alice-keycloak_target-pw-2!")
    assert error is None
    target_claims = target.verify(keycloak_target["issuer"], tokens_target["id_token"])
    assert sorted(source_claims["cognito:groups"]) == sorted(target_claims["groups"])
    # A Cognito refresh token means nothing to the new issuer.
    refreshed, error = target.refresh(keycloak_target["issuer"], tokens["RefreshToken"])
    assert refreshed is None and error not in (None, "unreachable")


def test_sign_in_continues_with_cognito_blocked(cognito, keycloak_target):
    """IAM-08: the source identity provider refuses every sign-in (its app client
    is gone); migrated users still sign in at the target."""
    assert migrate(cognito, keycloak_target)[0] == EffectStatus.APPLIED
    reset_password(keycloak_target, "bob", "Bob-keycloak_target-pw-2!")
    cognito["idp"].delete_user_pool_client(UserPoolId=cognito["pool"],
                                           ClientId=cognito["client"])
    blocked, error = cognito_login(cognito, "bob", cognito["passwords"]["bob"])
    assert blocked is None and error == "ResourceNotFoundException"
    passed, observation = OidcProbes(client_id=CLIENT).fresh_login(
        keycloak_target["issuer"], "bob", "Bob-keycloak_target-pw-2!")
    assert passed, observation


def test_an_account_disabled_in_cognito_is_disabled_at_the_next_sync(cognito, keycloak_target):
    adapter = KeycloakIdentityMigration("cognito-to-keycloak", source=cognito["directory"],
                                        target=keycloak_target["admin"], mfa_required=False)
    adapter.apply(command(adapter))
    cognito["idp"].admin_disable_user(UserPoolId=cognito["pool"], Username="bob")
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["mismatched"] == ["bob"]
    assert migrate(cognito, keycloak_target)[0] == EffectStatus.APPLIED
    assert keycloak_target["server"].find(keycloak_target["realm"], "bob")["enabled"] is False


def test_a_group_change_in_cognito_after_migration_reads_back_unknown(cognito, keycloak_target):
    adapter = KeycloakIdentityMigration("cognito-to-keycloak", source=cognito["directory"],
                                        target=keycloak_target["admin"], mfa_required=False)
    adapter.apply(command(adapter))
    cognito["idp"].admin_remove_user_from_group(UserPoolId=cognito["pool"], Username="bob",
                                                GroupName="support")
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["mismatched"] == ["bob"]
    assert migrate(cognito, keycloak_target)[0] == EffectStatus.APPLIED
