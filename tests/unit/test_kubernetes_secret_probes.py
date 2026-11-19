"""The Secret access decision with scripted API answers: the two views, the
positive control, and what is never a refusal."""

import pytest

from sova.adapters.kubernetes_secret_probes import (
    WORKLOADS,
    KubernetesSecretProbes,
    SecretAccessControls,
    ServiceAccount,
)
from sova.domain.contracts import ContractError

OTHER = ServiceAccount("orders", "billing")


class Answers(SecretAccessControls):
    """Scripted statuses per call; reviews allowed only where listed."""

    def __init__(self, *, get=403, list_=403, pod=403, allowed=(), missing=()):
        self.get, self.list_, self.pod = get, list_, pod
        self.allowed, self.missing = set(allowed), set(missing)

    def secret_status(self, name):
        return 404 if name in self.missing else self.get

    def list_status(self):
        return self.list_

    def pod_dry_run_status(self, secret):
        return self.pod

    def access_review(self, account, verb, group, resource, name=None):
        return f"{verb}:{resource if name is None else name}" in self.allowed


def probes(other=None, *, reader=None, secrets=("orders-db",)):
    return KubernetesSecretProbes(reader=reader or Answers(get=200), reviewer=other or Answers(),
                                  as_account=lambda account: other or Answers(),
                                  secrets=secrets, others=(OTHER,))


def test_refused_every_way_between_two_positive_controls_passes():
    passed, observation = probes().cross_workload_secret_denied()
    assert passed and observation["outcome"] == "refused"
    found = observation["principals"][OTHER.user]
    assert found["review_allowed"] == []
    assert set(found["attempt"]) == {"get:orders-db", "list:secrets",
                                     "create_pod_mounting_secret"}


@pytest.mark.parametrize("answers", [Answers(get=200), Answers(list_=200), Answers(pod=200)])
def test_any_attempt_that_is_served_fails(answers):
    answers.allowed = set()
    passed, observation = probes(answers).cross_workload_secret_denied()
    assert not passed and observation["outcome"] == "allowed"


@pytest.mark.parametrize("grant", ["watch:secrets", "patch:cronjobs", "update:statefulsets",
                                   "create:pods", "get:orders-db"])
def test_any_grant_the_api_server_confirms_fails_even_when_every_attempt_is_refused(grant):
    passed, observation = probes(Answers(allowed={grant})).cross_workload_secret_denied()
    assert not passed and observation["principals"][OTHER.user]["review_allowed"] == [grant]


def test_every_workload_kind_is_reviewed():
    seen = []

    class Recording(Answers):
        def access_review(self, account, verb, group, resource, name=None):
            seen.append((verb, group, resource))
            return False
    probes(Recording()).cross_workload_secret_denied()
    for group, resource in WORKLOADS:
        assert {(v, group, resource) for v in ("create", "update", "patch")} <= set(seen)
    assert {("list", "", "secrets"), ("watch", "", "secrets")} <= set(seen)


def test_a_token_the_api_server_does_not_accept_is_undecided():
    passed, observation = probes(Answers(get=401, list_=401, pod=401)
                                 ).cross_workload_secret_denied()
    assert not passed and observation["outcome"] == "undecided"


def test_without_the_secret_there_a_refusal_proves_nothing():
    passed, observation = probes(reader=Answers(get=200, missing={"orders-db"})
                                 ).cross_workload_secret_denied()
    assert not passed and observation["outcome"] == "undecided"


def test_a_registration_names_secrets_and_other_accounts():
    with pytest.raises(ContractError):
        probes(secrets=())
    with pytest.raises(ContractError):
        KubernetesSecretProbes(reader=Answers(), reviewer=Answers(), as_account=lambda a: None,
                               secrets=("orders-db",), others=())
    with pytest.raises(ContractError):
        ServiceAccount("Orders", "billing")
    with pytest.raises(ContractError):
        KubernetesSecretProbes(reader=object(), reviewer=Answers(), as_account=lambda a: None,
                               secrets=("orders-db",), others=(OTHER,))
