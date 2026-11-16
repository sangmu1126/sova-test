"""One unit whose state lives in several systems, handed off as one.

An application is rarely one store: a board keeps posts in SQLite and uploads
on a volume, its game server keeps matches in memory. The plan hands off the
unit as a whole (one authority, one write stop), so each action is performed
by every component adapter, in the registered order, and reads back as done
only when every component reads back as done. Each component sees the same
command under its own adapter ID; the evidence names every component's
verdict and what it saw.
"""

from dataclasses import replace

from sova.adapters.evidence_bounds import bounded
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json


class CompositeHandoffAdapter:
    def __init__(self, adapter_id, components):
        """``components``: ``(name, adapter)`` pairs, in the order actions run."""
        if type(components) is not tuple or len(components) < 2:
            raise ContractError("At least two named components required")
        names = [name for name, _ in components]
        if len(set(names)) != len(names) or not all(type(n) is str and n for n in names):
            raise ContractError("Distinct component names required")
        if len({adapter.adapter_id for _, adapter in components}) != len(components):
            raise ContractError("Each component has its own adapter ID")
        self.adapter_id, self.components = adapter_id, components

    @staticmethod
    def _for(adapter, command):
        return replace(command, adapter_id=adapter.adapter_id)

    def validate(self, command):
        if command.adapter_id != self.adapter_id:
            raise ContractError("Unsupported handoff action for this adapter")
        for _, adapter in self.components:
            adapter.validate(self._for(adapter, command))

    def apply(self, command):
        for _, adapter in self.components:
            adapter.apply(self._for(adapter, command))

    def readback(self, command, now):
        seen = {name: adapter.readback(self._for(adapter, command), now)
                for name, adapter in self.components}
        holds = all(o.status == EffectStatus.APPLIED for o in seen.values())
        detail = {"components": {name: {"status": o.status.value, **o.evidence}
                                 for name, o in seen.items()}}
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **bounded(detail)}, limit=8192))
