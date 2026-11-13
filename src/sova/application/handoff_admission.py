"""Admit an approved handoff plan: recheck it, then claim its scope atomically.

Admission sits between the existing approval and command issuance. It
recompiles from current authenticated inputs (``verify_approved_plan``),
refuses a plan whose inputs changed after its snapshot or whose change feeds
have a gap (INV-08), derives the absolute claim set, and records it in one
transaction that also locks the approval against revocation. The exact
frozen plan it admitted is stored by digest, so execution after a restart runs
that plan rather than a recompilation. Admission does not issue commands or an
execution permit; the existing execution ledger remains that authority.
"""

import json

from sova.domain.change_impact import ImpactAssessment
from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.plan_conflicts import lock_set
from sova.domain.plan_lowering import verify_approved_plan


class HandoffAdmission:
    def __init__(self, *, lifecycle, claims, changes, compiled, clock):
        if not callable(clock) or not callable(changes):
            raise ContractError("Trusted admission clock and change impact source required")
        if not callable(getattr(compiled, "save", None)):
            raise ContractError("Compiled plan store required")
        self.lifecycle, self.claims, self.changes, self.clock = (
            lifecycle, claims, changes, clock)
        self.compiled = compiled

    def admit(self, application_id, approval_id, *, recompile, verifiers):
        identifier(application_id)
        identifier(approval_id)
        stored = self.lifecycle.get_approval(application_id, approval_id)
        if stored.revoked:
            raise ContractError("Approval was revoked")
        plan = self.lifecycle.get_plan(application_id, stored.plan_id)
        revision, result = recompile()
        verify_approved_plan(
            plan,
            stored.approval,
            recompile=lambda: (revision, result),
            verifiers=verifiers,
            now=utc(self.clock()),
        )
        impact = self.changes(revision, plan, issued=0)
        if not isinstance(impact, ImpactAssessment) or impact.plan_digest != plan.fingerprint:
            raise ContractError("Change impact for another plan")
        impact.require_commands_allowed()
        locks, unresolved = lock_set(revision)
        if unresolved:
            raise ContractError("Plan scope cannot be resolved to claim keys")
        failback = json.loads(revision.canonical).get("failback_of")
        self.compiled.save(revision)  # content-addressed; harmless if admission fails
        claimed = self.claims.acquire(
            application_id=application_id,
            plan_id=plan.plan_id,
            plan_digest=plan.fingerprint,
            compiled_digest=revision.digest,
            approval_id=approval_id,
            approval_revision=stored.revision,
            claims=locks,
            supersedes=None if failback is None else failback["forward_plan_digest"],
        )
        return {
            "schema_version": 1,
            "status": "ADMITTED",
            "plan_id": plan.plan_id,
            "plan_digest": plan.fingerprint,
            "compiled_digest": revision.digest,
            "claims": claimed,
            "commands_issued": False,
            "authorizes_execution": False,
        }
