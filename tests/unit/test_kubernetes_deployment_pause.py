"""Deployment-controller pause decisions against stub controls (no cluster)."""

import pytest

from sova.adapters.kubernetes.handoff_controls import deployment_paused
from sova.domain.contracts import ContractError


class Controls:
    def __init__(self, wanted, replicas, alive, selector=None):
        self.found = {"metadata": {"resourceVersion": "7"},
                      "spec": {"replicas": wanted,
                               "selector": selector or {"matchLabels": {"app": "operator"}}},
                      "status": {"replicas": replicas}}
        self.alive = alive

    def deployment(self, name):
        return self.found

    def pods(self, selector):
        assert selector == {"app": "operator"}
        return set(self.alive)


@pytest.mark.parametrize("wanted,replicas,alive,paused", [
    (1, 1, ["op-1"], False), (0, 1, ["op-1"], False), (0, 0, ["op-1"], False),
    (0, 0, [], True)])
def test_paused_only_when_nothing_is_wanted_running_or_alive(wanted, replicas, alive, paused):
    assert deployment_paused(Controls(wanted, replicas, alive), "operator")[0] is paused


def test_a_selector_beyond_label_equality_is_not_guessed():
    expressions = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["x"]}]}
    with pytest.raises(ContractError):
        deployment_paused(Controls(0, 0, [], selector=expressions), "operator")
