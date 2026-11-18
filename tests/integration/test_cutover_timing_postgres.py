"""Read-only timing metadata from an actual admitted HTTP/SQL handoff."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest
from qualification_service import service_journey
from qualification_timing import ACTIONS, read_ledger
from test_order_http_handoff import http_business as http_business  # noqa: F401

from sova.adapters.postgres_evidence_observations import PostgresEvidenceObservations
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.evidence import EvidenceRecord, VerificationResult

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


def test_real_timing_is_plan_bound_and_does_not_export_unreferenced_observation_bodies(postgres_database, http_business, tmp_path):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        admission = h.approve_and_admit(compiled, 'timing-approval')
        h.worker().drive(h.tenant, h.app, admission['unit_digest'])
        assert h.status(admission['unit_digest'])['state'] == 'HANDOFF_COMPLETE'
        PostgresEvidenceObservations(h.connect, tenant_id=h.tenant).record(
            h.app, 'unreferenced', {'probe': 'restore_drill', 'private_value': 'must-not-leave-the-database'})
        # Direct fixture insertion, not an accepted producer/API path: same
        # revisions and obligation, but another unit's scope must stay out.
        plan = PostgresLifecycleRepository(h.connect, tenant_id=h.tenant).get_plan(h.app, admission['plan_id'])
        o = next(o for o in plan.obligations if o.obligation_id == 'residency.audit')
        foreign = PostgresEvidenceObservations(h.connect, tenant_id=h.tenant).record(
            h.app, o.obligation_id, {'probe': 'zone', 'private_value': 'another-unit-private-value'})
        at = h.clock() - timedelta(milliseconds=1)
        record = EvidenceRecord('timing-foreign-scope', o.obligation_id,
            ResourceScope(h.tenant, h.app, o.scope.environment_id, 'another-unit'), plan.revisions,
            h.settings.evidence_verifier, at, at+timedelta(minutes=1), VerificationResult.PASS, 'observation', foreign)
        PostgresSignedEvidence(h.connect, tenant_id=h.tenant).save_envelope(h.signer.sign(record, issued_at=at))
        before = h.status(admission['unit_digest'])
        ledger = read_ledger(h.connect, h.tenant, h.app, admission)
        assert h.status(admission['unit_digest']) == before
        assert ledger['snapshot']['read_only'] and ledger['snapshot']['isolation'] == 'repeatable read'
        assert len(ledger['effects']) == 21 and {e['action'] for e in ledger['effects']} == set(ACTIONS)
        assert len(ledger['authority']) == 10 and ledger['measurement_receipts']
        # Identical zone observations are content-addressed once. The signed
        # envelopes still represent three distinct obligations and timestamps.
        zone = [x for x in ledger['measurement_receipts'] if x['obligation_id'] in {
            'residency.audit', 'residency.backups', 'residency.business_data'}]
        assert len(zone) == 3 and len({x['artifact_digest'] for x in zone}) == 1
        assert ledger['scope']['operation_id'] == admission['operation_id']
        assert 'must-not-leave-the-database' not in json.dumps(ledger) and 'unreferenced' not in json.dumps(ledger)
        assert foreign not in json.dumps(ledger) and 'another-unit-private-value' not in json.dumps(ledger)
        assert all(set(e) == {'action', 'fingerprint', 'revision', 'status', 'at'} for e in ledger['effects'])
        assert not ledger['qualification_issued'] and not ledger['signature_authenticated_by_this_export']


@pytest.mark.parametrize('wrong', ['tenant', 'application', 'operation'])
def test_actual_other_scope_cannot_be_exported_as_the_admitted_plan(postgres_database, http_business, tmp_path, wrong):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        admission = h.approve_and_admit(h.register_forward(), 'timing-scope-approval')
        tenant, app, selected = h.tenant, h.app, deepcopy(admission)
        if wrong == 'tenant':
            tenant = 'another-tenant'
        elif wrong == 'application':
            app = selected['application_id'] = 'another-application'
        else:
            selected['operation_id'] = 'another-operation'
        with pytest.raises(ContractError, match='scope'):
            read_ledger(h.connect, tenant, app, selected)
