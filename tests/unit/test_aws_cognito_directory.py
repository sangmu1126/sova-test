"""Cognito directory decoding against a stub client (no AWS)."""

import pytest

from sova.adapters.aws_cognito_directory import CognitoUserPoolDirectory
from sova.adapters.keycloak_identity import AccountBoundExceeded
from sova.domain.contracts import ContractError

POOL = "ap-northeast-2_AbCdEf123"


class Stub:
    def __init__(self, users, groups):
        self.users, self.groups = users, groups

    def get_paginator(self, operation):
        stub = self

        class Pages:
            def paginate(self, **kwargs):
                assert kwargs["UserPoolId"] == POOL
                if operation == "list_users":
                    yield {"Users": stub.users[:2]}
                    yield {"Users": stub.users[2:]}
                else:
                    yield {"Groups": [{"GroupName": g}
                                      for g in stub.groups.get(kwargs["Username"], ())]}
        return Pages()


def user(name, sub, *, enabled=True, status="CONFIRMED", verified="true"):
    attributes = [{"Name": "email", "Value": f"{name}@example.test"},
                  {"Name": "email_verified", "Value": verified},
                  {"Name": "given_name", "Value": name.title()}]
    if sub:
        attributes.append({"Name": "sub", "Value": sub})
    return {"Username": name, "Attributes": attributes, "Enabled": enabled,
            "UserStatus": status}


def test_accounts_are_keyed_by_sub_with_groups_and_state():
    stub = Stub([user("Alice", "s-1"), user("bob", "s-2", enabled=False),
                 user("carol", "s-3", status="EXTERNAL_PROVIDER", verified="false")],
                {"Alice": ["finance"], "bob": ["support", "finance"]})
    directory = CognitoUserPoolDirectory(stub, POOL)
    accounts = {a["subject"]: a for a in directory.accounts(10)}
    assert accounts["s-1"]["username"] == "alice" and accounts["s-1"]["groups"] == ["/finance"]
    assert accounts["s-2"]["enabled"] is False
    assert accounts["s-2"]["groups"] == ["/finance", "/support"]
    assert accounts["s-3"]["federated"] and not accounts["s-3"]["emailVerified"]
    assert directory.issuer == f"https://cognito-idp.ap-northeast-2.amazonaws.com/{POOL}"


def test_a_user_without_a_subject_or_beyond_the_bound_is_refused():
    with pytest.raises(ContractError):
        CognitoUserPoolDirectory(Stub([user("x", None)], {}), POOL).accounts(10)
    with pytest.raises(AccountBoundExceeded):
        CognitoUserPoolDirectory(Stub([user(f"u{i}", f"s-{i}") for i in range(3)], {}),
                                 POOL).accounts(2)
    with pytest.raises(ContractError):
        CognitoUserPoolDirectory(Stub([], {}), "not-a-pool")
