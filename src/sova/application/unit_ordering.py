"""Ordering between handoff units of one service (ADR-0002).

A data unit whose contract moves the accounts off the source identity
provider activates only after the account unit completed. The compiler names
that unit (its digest and members) in the ``identity.unit_handoff_complete``
obligation; this verifier reads the unit's current authority record and
passes only for ``HANDOFF_COMPLETE``. Anything else - never started, still
running, held, aborted, or completed and later sent to failback - fails, and
the data unit is held before activation.
"""

from sova.domain.contracts import ContractError, digest, identifier
from sova.domain.handoff_authority import HandoffState


class UnitCompletion:
    def __init__(self, store):
        if not callable(getattr(store, "current", None)):
            raise ContractError("Authority store required")
        self.store = store

    def check(self, application_id, obligation):
        """``(passed, observation)`` for one ``unit_handoff_complete`` obligation."""
        if obligation.get("kind") != "unit_handoff_complete":
            raise ContractError("A unit completion obligation required")
        identifier(application_id)
        unit = digest(obligation["unit_digest"])
        record = self.store.current(application_id, unit)
        if record is None:
            return False, {"probe": "unit_handoff_complete", "unit_digest": unit,
                           "members": list(obligation.get("members", ())), "state": None}
        return record.state == HandoffState.HANDOFF_COMPLETE, {
            "probe": "unit_handoff_complete", "unit_digest": unit,
            "members": list(obligation.get("members", ())), "state": str(record.state),
            "epoch": record.epoch, "compiled_digest": record.compiled_digest,
            "owner_environment": record.owner_environment}
