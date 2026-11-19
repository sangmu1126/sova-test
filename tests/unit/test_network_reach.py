"""The reach decision with scripted client Pods: what counts as no reach,
and what is never a pass."""

import json

import pytest

from sova.adapters.kubernetes.network_reach import NetworkReachCheck
from sova.domain.contracts import ContractError


class Namespace:
    """Each created client answers the next scripted outcome (None: never)."""

    def __init__(self, *answers):
        self.answers, self.logs, self.created, self.deleted = list(answers), {}, [], []

    def create_pod(self, body):
        name = body["metadata"]["name"]
        self.created.append(body)
        answer = self.answers.pop(0)
        self.logs[name] = "" if answer is None else json.dumps({"reach": answer})

    def container_log(self, name, container):
        return self.logs[name]

    def pod(self, name):
        return {"status": {"phase": "Running"}}

    def delete_pod(self, name):
        self.deleted.append(name)


def check(inside, outside):
    return NetworkReachCheck(inside=inside, outside=[("other", outside)],
                             host="orders-db.orders.svc", port=5432,
                             client_labels={"role": "orders-client"},
                             image="python@sha256:" + "0" * 64, run_as=(10001, 10001),
                             start_seconds=0.5)


@pytest.mark.parametrize("dropped", ["refused", "timed_out"])
def test_no_reach_from_outside_between_two_connected_controls_passes(dropped):
    inside, outside = Namespace("connected", "connected"), Namespace(dropped)
    passed, observation = check(inside, outside).measure()
    assert passed and observation["outcome"] == "denied"
    assert [p["metadata"]["labels"]["role"] for p in inside.created] == ["orders-client"] * 2
    assert "role" not in outside.created[0]["metadata"]["labels"]
    assert len(inside.deleted) == 2 and len(outside.deleted) == 1


def test_a_connection_from_outside_fails_whatever_the_controls_say():
    passed, observation = check(Namespace("timed_out", "timed_out"),
                                Namespace("connected")).measure()
    assert not passed and observation["outcome"] == "reached"


@pytest.mark.parametrize("inside, outside", [
    (("timed_out", "connected"), ("timed_out",)),   # the endpoint was not reachable at all
    (("connected", "connected"), (None,)),          # the outside client never answered
    (("connected", "connected"), ("error:gaierror",)),
])
def test_anything_short_of_a_bracketed_refusal_is_undecided(inside, outside):
    passed, observation = check(Namespace(*inside), Namespace(*outside)).measure()
    assert not passed and observation["outcome"] == "undecided"


def test_a_registration_names_the_endpoint_and_other_namespaces():
    with pytest.raises(ContractError):
        NetworkReachCheck(inside=None, outside=[], host="db", port=5432,
                          client_labels={"a": "b"}, image="i", run_as=(1, 1))
    with pytest.raises(ContractError):
        NetworkReachCheck(inside=None, outside=[("o", None)], host="Bad_Host", port=5432,
                          client_labels={"a": "b"}, image="i", run_as=(1, 1))
