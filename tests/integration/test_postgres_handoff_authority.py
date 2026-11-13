"""Persisted authority transitions driven by real signed evidence in PostgreSQL.

Synthetic contracts, local Ed25519 keys, and simulated effect outcomes; no
live environment is changed and no adapter command is executed.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier

from handoff_support import admitted_handoff
import pytest

from sova.domain.contracts import ContractError
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.ports.plans import RepositoryConflict

pytestmark = pytest.mark.postgres


@pytest.fixture
def handoff(postgres_database, now):
    return admitted_handoff(postgres_database, now)


def step(h, op, outcome=EffectOutcome.EFFECT_CONFIRMED):
    return h["coordinator"].apply_effect(h["revision"], h["plan"],
                                         operation_id=op["operation_id"],
                                         outcome=outcome, actor_id="coordinator")


def test_signed_evidence_drives_the_handoff_to_completion(handoff):
    h = handoff
    record = h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    assert record.state == HandoffState.SOURCE_ACTIVE
    for op in h["ops"]:
        if op["required_evidence"]:
            with pytest.raises(ContractError, match="evidence"):
                step(h, op)
        for obligation_id in op["required_evidence"]:
            h["prove"](obligation_id)
        record = step(h, op)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert record.owner_environment == record.target_environment
    history = h["store"].history(h["app"], record.unit_digest)
    assert [rev for rev, *_ in history] == list(range(1, len(h["ops"]) + 2))


def test_foreign_evidence_does_not_satisfy_a_step(handoff):
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first = h["ops"][0]
    target = first["required_evidence"][0]
    for obligation_id in first["required_evidence"]:
        if obligation_id != target:
            h["prove"](obligation_id)
    other = replace(h["plan"].revisions, policy="9" * 64)
    h["prove"](target, revisions=other)  # bound to another plan's inputs
    with pytest.raises(ContractError, match="evidence"):
        step(h, first)
    h["prove"](target)
    assert step(h, first).state == HandoffState.TARGET_PREPARED


def test_newer_failed_evidence_is_a_contract_breach_not_a_retry(handoff):
    """A signed FAIL newer than the PASS is a violated condition (§19.1): the
    step is held as a contract breach, and a later PASS does not resume it."""
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first = h["ops"][0]
    target = first["required_evidence"][0]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    h["prove"](target, VerificationResult.FAIL)
    held = step(h, first)
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "CONTRACT_BREACH"
    h["prove"](target)
    with pytest.raises(ContractError, match="SAFE_HOLD"):
        step(h, first)


def test_unknown_effect_holds_and_persists_until_reconciled(handoff):
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first, second = h["ops"][:2]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    held = step(h, first, EffectOutcome.EFFECT_UNKNOWN)
    assert held.state == HandoffState.SAFE_HOLD
    assert h["store"].current(h["app"], held.unit_digest) == held
    with pytest.raises(ContractError, match="SAFE_HOLD"):
        step(h, second)
    resumed = h["coordinator"].reconcile(h["revision"], h["plan"],
                                         outcome=EffectOutcome.EFFECT_CONFIRMED,
                                         actor_id="coordinator")
    assert resumed.state == HandoffState.TARGET_PREPARED


def test_concurrent_coordinators_cannot_both_advance(handoff):
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first = h["ops"][0]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    start = Barrier(2)

    def race(_):
        start.wait()
        try:
            return str(step(h, first).state)
        except (RepositoryConflict, ContractError):
            return "REFUSED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(race, range(2)))
    assert outcomes == ["REFUSED", "TARGET_PREPARED"]
    record = h["store"].current(h["app"], json.loads(h["revision"].canonical)["unit"]["unit_digest"])
    assert record.revision == 2


def test_released_claims_and_unfinished_units_refuse_changes(handoff):
    h = handoff
    record = h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    with pytest.raises(RepositoryConflict, match="unfinished"):
        h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    h["claims"].release(application_id=h["app"], plan_digest=h["plan"].fingerprint,
                        actor_id="coordinator")
    with pytest.raises(RepositoryConflict, match="not admitted"):
        h["coordinator"].abort(h["revision"], h["plan"], target_inactive_verified=True,
                               actor_id="coordinator")
    assert h["store"].current(h["app"], record.unit_digest) == record


def open_hold(h, hold_id="incident-1"):
    from datetime import UTC, datetime, timedelta

    from sova.adapters.postgres_governance import PostgresSecurityHolds
    from sova.domain.contracts import ResourceScope
    from sova.domain.governance import SecurityHold

    return PostgresSecurityHolds(h["connect"], tenant_id=h["tenant"]).open_hold(
        SecurityHold(hold_id, ResourceScope(h["tenant"], h["app"], "aws-source", "api-a"),
                     "incident-commander", "Suspected credential compromise",
                     datetime.now(UTC) - timedelta(seconds=1)))


def test_security_hold_allows_only_safety_ward_transitions(handoff):
    h = handoff
    record = h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first = h["ops"][0]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    open_hold(h)
    with pytest.raises(RepositoryConflict, match="security hold"):
        step(h, first)  # forward progress is refused
    with pytest.raises(RepositoryConflict, match="security hold"):
        h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op",
                                      epoch=1)
    with pytest.raises(RepositoryConflict, match="security hold"):
        h["coordinator"].abort(h["revision"], h["plan"], target_inactive_verified=True,
                               actor_id="coordinator")  # no source reopen under a hold
    held = step(h, first, EffectOutcome.EFFECT_UNKNOWN)  # toward safety is recorded
    assert held.state == HandoffState.SAFE_HOLD
    assert h["store"].current(h["app"], record.unit_digest) == held


def test_security_hold_blocks_admission_and_opening(postgres_database, now):
    from handoff_support import admitted_handoff as build

    h = build(postgres_database, now)
    open_hold(h)
    with pytest.raises(RepositoryConflict, match="security hold"):
        h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
