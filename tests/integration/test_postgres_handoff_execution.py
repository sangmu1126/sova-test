"""Compiled handoff executed through the existing PostgreSQL effect ledger.

Approval -> admission -> authority record -> next-step command -> existing
ownership lease and EffectExecutor -> ledger readback -> authority transition.
The adapter is a recording double; no real environment is changed.
"""

from datetime import UTC, datetime

from handoff_support import admitted_handoff
import pytest
from support import permit_for

from sova.adapters.postgres_effects import PostgresEffectJournal
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.application.effects import EffectExecutor, UncertainEffect
from sova.domain.contracts import (
    ContractError,
    EffectObservation,
    EffectStatus,
    canonical_json,
)
from sova.domain.handoff_authority import HandoffState
from sova.domain.operations import OperationRequest

pytestmark = pytest.mark.postgres
OPERATION = "handoff-operation"


class RecordingAdapter:
    """Accepts exactly the compiled operation kinds; readback is configurable."""

    def __init__(self, adapter_id, kinds):
        self.adapter_id, self.kinds = adapter_id, kinds
        self.calls, self.uncertain = [], set()

    def validate(self, command):
        if command.action not in self.kinds:
            raise ContractError("Unregistered handoff action")

    def apply(self, command):
        self.calls.append(command.action)

    def readback(self, command, now):
        status = (
            EffectStatus.UNKNOWN if command.action in self.uncertain else EffectStatus.APPLIED
        )
        return EffectObservation(command.scope, command.fingerprint, status, now,
                                 canonical_json({"action": command.action}))


@pytest.fixture
def execution(postgres_database, now):
    h = admitted_handoff(postgres_database, now)
    lifecycle = h["lifecycle"]
    approval = lifecycle.get_approval(h["app"], "approval-1")
    PostgresOperationRepository(lifecycle).submit(OperationRequest(
        h["tenant"], h["app"], OPERATION, h["plan"].plan_id, h["plan"].fingerprint,
        "approval-1", approval.revision, "operator", "handoff-request"))
    ownership = PostgresExecutionOwnership(lifecycle)
    lease = ownership.acquire(h["app"], OPERATION, "worker")
    journal = PostgresEffectJournal(ownership, lease)
    engine = EffectExecutor(journal, authorize=journal.authorize,
                            clock=lambda: datetime.now(UTC))
    kinds = {op["kind"] for op in h["ops"] if op["adapter"]}
    adapter = RecordingAdapter(h["world"]["manifest"].adapter_id, kinds)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    return dict(h, lease=lease, engine=engine, adapter=adapter, journal=journal)


def run_step(x, op):
    for obligation_id in op["required_evidence"]:
        x["prove"](obligation_id)
    effect = None
    if op["adapter"]:
        command = x["coordinator"].next_command(x["revision"], x["plan"],
                                                operation_id=OPERATION,
                                                epoch=x["lease"].epoch)
        effect = x["engine"].execute(command, permit_for(command), x["adapter"])
    return x["coordinator"].advance_from_ledger(x["revision"], x["plan"], effect=effect,
                                                actor_id="coordinator")


def test_full_handoff_runs_through_the_existing_effect_ledger(execution):
    x = execution
    for op in x["ops"]:
        record = run_step(x, op)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert x["adapter"].calls == [op["kind"] for op in x["ops"] if op["adapter"]]
    # Every adapter effect is in the shared ledger as confirmed_applied.
    for op in x["ops"]:
        if op["adapter"]:
            command = x["plan"].command(op["operation_id"], operation_id=OPERATION,
                                        epoch=x["lease"].epoch)
            assert x["journal"].get(command).status == EffectStatus.APPLIED


def test_uncertain_fence_readback_holds_and_issues_no_further_command(execution):
    x = execution
    x["adapter"].uncertain.add("fence_source_writes")  # command action = step kind
    for op in x["ops"]:
        if op["kind"] == "fence_source_writes":
            fence = op
            break
        run_step(x, op)
    for obligation_id in fence["required_evidence"]:
        x["prove"](obligation_id)
    command = x["coordinator"].next_command(x["revision"], x["plan"],
                                            operation_id=OPERATION, epoch=x["lease"].epoch)
    with pytest.raises(UncertainEffect):
        x["engine"].execute(command, permit_for(command), x["adapter"])
    effect = x["journal"].get(command)
    assert effect.status == EffectStatus.UNKNOWN
    held = x["coordinator"].advance_from_ledger(x["revision"], x["plan"], effect=effect,
                                                actor_id="coordinator")
    assert held.state == HandoffState.SAFE_HOLD
    assert not held.activation_possible
    with pytest.raises(ContractError, match="does not allow"):
        x["coordinator"].next_command(x["revision"], x["plan"], operation_id=OPERATION,
                                      epoch=x["lease"].epoch)
