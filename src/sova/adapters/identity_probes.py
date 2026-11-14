"""Independent probes for identity mapping obligations (IAM-01, IAM-06).

The migration adapter decides from two bulk listings. These probes decide the
other way round and share none of its code: for every source account they ask
the target for exactly the users that claim that source subject (an attribute
search), and then compare the one they find. A defect in the adapter's
listing or matching therefore does not repeat itself here.

* ``subject_mapping_unique`` - every enabled source account is claimed by
  exactly one target user; none is unclaimed or claimed twice.
* ``account_state_parity`` - each claimed pair agrees on username, e-mail,
  enabled and e-mail-verified state and group membership.

Each returns ``(passed, observation)``; observations carry usernames and
counts, never credentials or attribute values beyond the usernames.
"""

from sova.adapters.keycloak_identity import SUBJECT, KeycloakAdmin
from sova.domain.contracts import ContractError


class IdentityMappingProbes:
    def __init__(self, *, source, target, bound=10_000):
        if not callable(getattr(source, "accounts", None)) or not isinstance(target,
                                                                              KeycloakAdmin):
            raise ContractError("A source directory and the target realm required")
        self.source, self.target, self.bound = source, target, bound

    def _claims(self, realm, subject):
        return realm.get("/users", q=f"{SUBJECT}:{subject}", briefRepresentation="false",
                         exact="true", max=3)

    def _pairs(self):
        accounts = [a for a in self.source.accounts(self.bound) if not a["federated"]]
        with self.target.session() as realm:
            for account in accounts:
                claims = [u for u in self._claims(realm, account["subject"])
                          if (u.get("attributes") or {}).get(SUBJECT) == [account["subject"]]]
                groups = (sorted(g["path"] for g in realm.get(f"/users/{claims[0]['id']}/groups"))
                          if len(claims) == 1 else None)
                yield account, claims, groups

    def subject_mapping_unique(self):
        unclaimed, duplicated, checked = [], [], 0
        for account, claims, _ in self._pairs():
            if not account["enabled"] and not claims:
                continue  # a disabled source account need not exist at the target
            checked += 1
            if not claims:
                unclaimed.append(account["username"])
            elif len(claims) > 1:
                duplicated.append(account["username"])
        return not unclaimed and not duplicated and checked > 0, {
            "probe": "subject_mapping_unique", "checked": checked,
            "unclaimed": unclaimed[:20], "claimed_twice": duplicated[:20]}

    def account_state_parity(self):
        differing, compared = [], 0
        for account, claims, groups in self._pairs():
            if len(claims) != 1:
                continue
            user = claims[0]
            compared += 1
            same = (user["username"] == account["username"]
                    and user.get("email") == account.get("email")
                    and user["enabled"] == account["enabled"]
                    and user.get("emailVerified", False) == account.get("emailVerified", False)
                    and groups == sorted(account["groups"]))
            if not same:
                differing.append(account["username"])
        return not differing and compared > 0, {"probe": "account_state_parity",
                                                "compared": compared,
                                                "differing": differing[:20]}
