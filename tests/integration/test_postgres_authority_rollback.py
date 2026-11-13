"""Authority database restored to an earlier point (AT-33, §15.6).

The restore is simulated by an administrator rewriting the authority row to
an earlier revision from its own history; the witness is a hash-chained file.
Synthetic inputs and local keys; no live environment is changed.
"""

import json

from handoff_support import admitted_handoff
import psycopg
from psycopg.types.json import Jsonb
import pytest

from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.domain.contracts import ContractError
from sova.domain.handoff_authority import (
    ROLLBACK_RECOVERY,
    AuthorityRollbackDetected,
    EffectOutcome,
    HandoffState,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def running(postgres_database, now, tmp_path):
    h = admitted_handoff(postgres_database, now, witness_for=lambda tenant: (
        FileAuthorityWitness(tmp_path / "authority-witness.jsonl", tenant_id=tenant)))
    h["admin"] = postgres_database[0]
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    for op in h["ops"]:
        if op["kind"] == "copy_content":
            break
        for obligation_id in op["required_evidence"]:
            h["prove"](obligation_id)
        h["coordinator"].apply_effect(h["revision"], h["plan"],
                                      operation_id=op["operation_id"],
                                      outcome=EffectOutcome.EFFECT_CONFIRMED,
                                      actor_id="coordinator")
    h["unit"] = json.loads(h["revision"].canonical)["unit"]["unit_digest"]
    return h


def restore(h, revision=None):
    """What a point-in-time restore leaves behind: an earlier row, or none."""
    with psycopg.connect(h["admin"]) as conn:
        if revision is None:
            conn.execute("DELETE FROM sova_control.handoff_authority "
                         "WHERE tenant_id=%s AND unit_digest=%s", (h["tenant"], h["unit"]))
            return
        old = next(r for rev, _, _, r in h["store"].history(h["app"], h["unit"])
                   if rev == revision)
        conn.execute(
            "UPDATE sova_control.handoff_authority SET revision=%s,state=%s,document=%s "
            "WHERE tenant_id=%s AND unit_digest=%s",
            (old.revision, str(old.state), Jsonb(old.document), h["tenant"], h["unit"]))


def test_restored_authority_stops_commands_until_a_recovery_incarnation(running):
    h = running
    latest = h["witness"].latest(h["app"], h["unit"])
    restore(h, revision=2)
    with pytest.raises(AuthorityRollbackDetected):
        h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op", epoch=1)
    with pytest.raises(AuthorityRollbackDetected):
        h["store"].current(h["app"], h["unit"])
    recovered = h["store"].recover(h["app"], h["unit"], h["revision"], actor_id="recovery")
    assert recovered.state == HandoffState.SAFE_HOLD and recovered.reason == ROLLBACK_RECOVERY
    assert recovered.held_from == HandoffState(latest["state"])
    assert recovered.epoch == latest["epoch"] + 1 and recovered.fence_verified
    assert h["store"].current(h["app"], h["unit"]) == recovered
    assert h["witness"].latest(h["app"], h["unit"]) == recovered.document
    with pytest.raises(ContractError, match="does not allow"):
        h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op", epoch=1)
    # Compare with the field: a fresh observation of the held step resumes it.
    copy = next(op for op in h["ops"] if op["kind"] == "copy_content")
    for obligation_id in copy["required_evidence"]:
        h["prove"](obligation_id)
    resumed = h["coordinator"].reconcile(h["revision"], h["plan"],
                                         outcome=EffectOutcome.EFFECT_NOT_APPLIED,
                                         actor_id="coordinator")
    assert resumed.state == HandoffState(latest["state"])
    command = h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op",
                                            epoch=1)
    assert command.action == "copy_content"


def test_a_lost_row_cannot_be_reopened_as_new(running):
    h = running
    restore(h)  # restored to before the handoff was opened
    with pytest.raises(AuthorityRollbackDetected):
        h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    recovered = h["store"].recover(h["app"], h["unit"], h["revision"], actor_id="recovery")
    assert recovered.state == HandoffState.SAFE_HOLD
    assert h["store"].history(h["app"], h["unit"])[-1][1] == "recovery_incarnation"
