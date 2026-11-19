"""Logical proof boundary tests; these never qualify a deployed observer."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sova.domain.contracts import ContractError
from sova.domain.execution import pending_digest
from sova.service.quiescence import KubernetesWorkerQuiescence


class Lifecycle:
    def __init__(self, row):
        self.row, self.calls = row, []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, values):
        self.calls.append((query, values))
        return self

    def fetchone(self):
        return self.row


def world():
    now = datetime.now(UTC)
    owner = f"worker-a.{uuid4()}.0123456789ab"
    return now, owner, Lifecycle((owner, 3, now-timedelta(seconds=1), now))


@pytest.mark.parametrize("state", (False, True, None))
def test_no_registered_remote_observer_never_proves_even_a_gone_pod(state):
    now, _, lifecycle = world()
    verifier = KubernetesWorkerQuiescence(pod_running=lambda *_: state, clock=lambda: now)
    assert verifier.prove(lifecycle, "tenant", "app", "operation", ("a"*64,)) is None
    assert not lifecycle.calls and not verifier._issued


@pytest.mark.parametrize("answer", (False, None, 1, "true", {}, []))
def test_unknown_truthy_or_false_remote_observation_never_authorizes(answer):
    now, _, lifecycle = world()
    verifier = KubernetesWorkerQuiescence(pod_running=lambda *_: False, clock=lambda: now,
        requests_settled=lambda **_: answer)
    assert verifier.prove(lifecycle, "tenant", "app", "operation", ("a"*64,)) is None
    assert not verifier._issued


def test_remote_failure_is_uncertain_instead_of_a_recovery_right():
    now, _, lifecycle = world()
    def unavailable(**kwargs):
        raise TimeoutError("observer unavailable")
    verifier = KubernetesWorkerQuiescence(pod_running=lambda *_: False, clock=lambda: now,
        requests_settled=unavailable)
    assert verifier.prove(lifecycle, "tenant", "app", "operation", ("a"*64,)) is None


def test_exact_owner_epoch_operation_pending_binding_and_half_open_expiry():
    now, owner, lifecycle = world()
    calls = []
    def settled(**scope):
        calls.append(scope)
        return True
    current = [now]
    verifier = KubernetesWorkerQuiescence(pod_running=lambda *_: False, clock=lambda: current[0],
        requests_settled=settled)
    pending = ("a"*64,)
    proof = verifier.prove(lifecycle, "tenant", "app", "operation", pending)
    assert proof is not None
    assert calls == [{"tenant_id":"tenant","application_id":"app","operation_id":"operation",
        "owner_id":owner,"epoch":3,"pending_digest":pending_digest(pending)}]
    query, values = lifecycle.calls[0]
    assert "operation_id=%s" in query and values == ("tenant","app","operation")
    verifier.authenticate(proof)
    current[0] = proof.valid_until
    with pytest.raises(ContractError, match="expired"):
        verifier.authenticate(proof)


def test_running_pod_never_calls_the_remote_observer():
    now, _, lifecycle = world()
    def forbidden(**scope):
        raise AssertionError("Running owner must not be observed as settled")
    verifier = KubernetesWorkerQuiescence(pod_running=lambda *_: True, clock=lambda: now,
        requests_settled=forbidden)
    assert verifier.prove(lifecycle, "tenant", "app", "operation", ("a"*64,)) is None


@pytest.mark.parametrize("validity", (timedelta(0),timedelta(seconds=-1),timedelta(minutes=3),1))
def test_proof_validity_cannot_exceed_the_platform_window(validity):
    with pytest.raises(ContractError):
        KubernetesWorkerQuiescence(pod_running=lambda *_: False, clock=lambda: None, validity=validity)
