"""AWS Cognito (the cloud identity provider) to Keycloak (on premises), measured.

A run-scoped user pool in the real AWS account holds synthetic users with real
passwords and groups. Accounts move to a local Keycloak realm without their
credentials; the probes sign in at both sides for real and verify the ID
tokens against each issuer's published keys (Cognito's own JWKS included).
No e-mail is sent: users are created with message delivery suppressed.
"""

from datetime import UTC, datetime, timedelta
import json
import secrets
from uuid import uuid4

from keycloak_fixtures import CLIENT
import pytest

from sova.adapters.aws_cognito_directory import CognitoUserPoolDirectory
from sova.adapters.keycloak_identity import SUBJECT, KeycloakAdmin, KeycloakIdentityMigration
from sova.adapters.oidc_probes import OidcProbes
from sova.domain.contracts import EffectCommand, EffectStatus, ResourceScope

pytestmark = [pytest.mark.aws_live, pytest.mark.keycloak]


def _password():
    return "Sv1!" + secrets.token_urlsafe(18)


@pytest.fixture
def cognito(aws):
    idp = aws["session"].client("cognito-idp")
    pool = idp.create_user_pool(
        PoolName=f"sova-test-pool-{aws['run']}-{uuid4().hex[:6]}", UserPoolTier="LITE",
        MfaConfiguration="OFF", DeletionProtection="INACTIVE",
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        UserPoolTags={t["Key"]: t["Value"] for t in aws["tags"]},
        Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True,
                                     "RequireLowercase": True, "RequireNumbers": True,
                                     "RequireSymbols": True}})["UserPool"]["Id"]
    try:
        client = idp.create_user_pool_client(
            UserPoolId=pool, ClientName="orders-web", GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        )["UserPoolClient"]["ClientId"]
        for group in ("finance", "support"):
            idp.create_group(UserPoolId=pool, GroupName=group)
        passwords = {}
        for name, groups, enabled in (("alice", ("finance",), True),
                                      ("bob", ("finance", "support"), True),
                                      ("carol", (), False)):
            idp.admin_create_user(
                UserPoolId=pool, Username=name, MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": f"{name}@example.test"},
                                {"Name": "email_verified", "Value": "true"},
                                {"Name": "given_name", "Value": name.title()},
                                {"Name": "family_name", "Value": "Tester"}])
            passwords[name] = _password()
            idp.admin_set_user_password(UserPoolId=pool, Username=name,
                                        Password=passwords[name], Permanent=True)
            for group in groups:
                idp.admin_add_user_to_group(UserPoolId=pool, Username=name, GroupName=group)
            if not enabled:
                idp.admin_disable_user(UserPoolId=pool, Username=name)
        yield {"idp": idp, "pool": pool, "client": client, "passwords": passwords,
               "directory": CognitoUserPoolDirectory(idp, pool)}
    finally:
        idp.delete_user_pool(UserPoolId=pool)


def cognito_login(c, username, password):
    """Cognito has no password grant at its token endpoint; its API signs in."""
    try:
        result = c["idp"].initiate_auth(
            ClientId=c["client"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password})
    except c["idp"].exceptions.ClientError as error:
        return None, error.response["Error"]["Code"]
    return result["AuthenticationResult"], None


@pytest.fixture
def onprem(keycloak_servers, realm_name):
    issuer = keycloak_servers["target"].realm(realm_name)
    user, password = keycloak_servers["admin"]
    admin = KeycloakAdmin(keycloak_servers["target"].url, realm_name, username=user,
                          password=password)
    return {"server": keycloak_servers["target"], "realm": realm_name, "issuer": issuer,
            "admin": admin}


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-identity", "app-identity", "onprem-target", "unit-identity"),
        "operation-identity", "migrate", adapter.adapter_id, "migrate_identities",
        "operation-identity:migrate", "d" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), json.dumps({"identity": True}))


def migrate(cognito, onprem):
    adapter = KeycloakIdentityMigration("cognito-to-keycloak", source=cognito["directory"],
                                        target=onprem["admin"], mfa_required=False)
    adapter.apply(command(adapter))
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    return observation.status, observation.evidence


def reset(onprem, username, password):
    found = onprem["server"].find(onprem["realm"], username)
    onprem["server"].call("PUT", f"/{onprem['realm']}/users/{found['id']}/reset-password",
                          {"type": "password", "value": password, "temporary": False})
    onprem["server"].call("PUT", f"/{onprem['realm']}/users/{found['id']}",
                          {**found, "requiredActions": []})
    return found


def test_cognito_accounts_move_to_keycloak_with_groups_but_without_passwords(cognito,
                                                                          onprem):
    status, evidence = migrate(cognito, onprem)
    assert status == EffectStatus.APPLIED, evidence
    assert evidence["source_accounts"] == 3 and evidence["federated_accounts"] == []
    tokens, error = cognito_login(cognito, "alice", cognito["passwords"]["alice"])
    assert error is None
    probes = OidcProbes(client_id=cognito["client"])
    source_claims = probes.verify(cognito["directory"].issuer, tokens["IdToken"])
    alice = onprem["server"].find(onprem["realm"], "alice")
    assert alice["attributes"][SUBJECT] == [source_claims["sub"]]  # (issuer, sub) kept
    assert onprem["server"].find(onprem["realm"], "carol") is None  # disabled: not created

    target = OidcProbes(client_id=CLIENT)
    passed, observation = target.fresh_login(onprem["issuer"], "alice",
                                             cognito["passwords"]["alice"])
    assert not passed and observation["outcome"] == "invalid_grant"  # no password moved
    reset(onprem, "alice", "Alice-onprem-pw-2!")
    tokens_target, error = target.login(onprem["issuer"], "alice", "Alice-onprem-pw-2!")
    assert error is None
    target_claims = target.verify(onprem["issuer"], tokens_target["id_token"])
    assert sorted(source_claims["cognito:groups"]) == sorted(target_claims["groups"])
    # A Cognito refresh token means nothing to the new issuer.
    refreshed, error = target.refresh(onprem["issuer"], tokens["RefreshToken"])
    assert refreshed is None and error not in (None, "unreachable")


def test_sign_in_continues_with_cognito_blocked(cognito, onprem):
    """IAM-08: the source identity provider refuses every sign-in (its app client
    is gone); migrated users still sign in at the target."""
    assert migrate(cognito, onprem)[0] == EffectStatus.APPLIED
    reset(onprem, "bob", "Bob-onprem-pw-2!")
    cognito["idp"].delete_user_pool_client(UserPoolId=cognito["pool"],
                                           ClientId=cognito["client"])
    blocked, error = cognito_login(cognito, "bob", cognito["passwords"]["bob"])
    assert blocked is None and error == "ResourceNotFoundException"
    passed, observation = OidcProbes(client_id=CLIENT).fresh_login(
        onprem["issuer"], "bob", "Bob-onprem-pw-2!")
    assert passed, observation


def test_an_account_disabled_in_cognito_is_disabled_at_the_next_sync(cognito, onprem):
    adapter = KeycloakIdentityMigration("cognito-to-keycloak", source=cognito["directory"],
                                        target=onprem["admin"], mfa_required=False)
    adapter.apply(command(adapter))
    cognito["idp"].admin_disable_user(UserPoolId=cognito["pool"], Username="bob")
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["mismatched"] == ["bob"]
    assert migrate(cognito, onprem)[0] == EffectStatus.APPLIED
    assert onprem["server"].find(onprem["realm"], "bob")["enabled"] is False


def test_a_group_change_in_cognito_after_migration_reads_back_unknown(cognito, onprem):
    adapter = KeycloakIdentityMigration("cognito-to-keycloak", source=cognito["directory"],
                                        target=onprem["admin"], mfa_required=False)
    adapter.apply(command(adapter))
    cognito["idp"].admin_remove_user_from_group(UserPoolId=cognito["pool"], Username="bob",
                                                GroupName="support")
    observation = adapter.readback(command(adapter), datetime.now(UTC))
    assert observation.status == EffectStatus.UNKNOWN
    assert observation.evidence["mismatched"] == ["bob"]
    assert migrate(cognito, onprem)[0] == EffectStatus.APPLIED
