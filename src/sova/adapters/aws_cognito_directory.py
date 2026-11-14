"""An AWS Cognito user pool as an identity migration source (Part II §5-§7).

Reads only, with two calls: ``ListUsers`` and ``AdminListGroupsForUser``.
Cognito never returns passwords, password hashes or MFA secrets, and nothing
here asks for them; moving out of Cognito therefore always means a new
credential at the target (``RESET_AND_REENROLL``). Cognito's user migration
trigger works in the other direction (into Cognito) and is not used.

The stable subject is the ``sub`` attribute: immutable and unique within the
pool, unlike the username or the e-mail address, which must never be used to
merge accounts. Together with ``issuer`` it identifies the source identity.

A user whose status is ``EXTERNAL_PROVIDER`` signs in at a federated identity
provider; a password reset cannot move it, so it is reported as federated and
left to a federation plan.
"""

import re

from sova.adapters.keycloak_identity import AccountBoundExceeded
from sova.domain.contracts import ContractError

_POOL = re.compile(r"[a-z]{2}-[a-z]+-[0-9]_[0-9A-Za-z]{1,55}")


class CognitoUserPoolDirectory:
    def __init__(self, client, user_pool_id):
        if not callable(getattr(client, "get_paginator", None)):
            raise ContractError("Cognito identity provider client required")
        if type(user_pool_id) is not str or not _POOL.fullmatch(user_pool_id):
            raise ContractError("Cognito user pool ID required")
        self.client, self.pool = client, user_pool_id

    @property
    def issuer(self):
        region = self.pool.split("_", 1)[0]
        return f"https://cognito-idp.{region}.amazonaws.com/{self.pool}"

    def _groups(self, username):
        pages = self.client.get_paginator("admin_list_groups_for_user").paginate(
            UserPoolId=self.pool, Username=username)
        return sorted("/" + g["GroupName"] for page in pages for g in page.get("Groups", ()))

    def accounts(self, bound):
        accounts = []
        for page in self.client.get_paginator("list_users").paginate(UserPoolId=self.pool):
            for user in page.get("Users", ()):
                if len(accounts) >= bound:
                    raise AccountBoundExceeded("More Cognito users than the bound")
                attributes = {a["Name"]: a["Value"] for a in user.get("Attributes", ())}
                if not attributes.get("sub"):
                    raise ContractError("Cognito user without a subject")
                accounts.append({
                    "subject": attributes["sub"], "username": user["Username"].lower(),
                    "email": attributes.get("email"),
                    "firstName": attributes.get("given_name"),
                    "lastName": attributes.get("family_name"), "enabled": user["Enabled"],
                    "emailVerified": attributes.get("email_verified") == "true",
                    "groups": self._groups(user["Username"]),
                    "federated": user.get("UserStatus") == "EXTERNAL_PROVIDER"})
        return accounts
