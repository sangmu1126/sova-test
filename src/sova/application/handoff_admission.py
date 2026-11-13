"""Admit an approved handoff plan: recheck it, then claim its scope atomically.

Admission sits between the existing approval and command issuance. It
recompiles from current authenticated inputs (``verify_approved_plan``),
derives the absolute claim set, and records it in one transaction that also
locks the approval against revocation. Admission does not issue commands or an
execution permit; the existing execution ledger remains that authority.
"""

from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.plan_conflicts import lock_set
from sova.domain.plan_lowering import verify_approved_plan


class HandoffAdmission:
    def __init__(self, *, lifecycle, claims, clock):
        if not callable(clock):
            raise ContractError("Trusted admission clock required")
        self.lifecycle, self.claims, self.clock = lifecycle, claims, clock

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
        locks, unresolved = lock_set(revision)
        if unresolved:
            raise ContractError("Plan scope cannot be resolved to claim keys")
        claimed = self.claims.acquire(
            application_id=application_id,
            plan_id=plan.plan_id,
            plan_digest=plan.fingerprint,
            compiled_digest=revision.digest,
            approval_id=approval_id,
            approval_revision=stored.revision,
            claims=locks,
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
