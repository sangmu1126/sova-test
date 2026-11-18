"""Read-only timing metadata from an actual admitted HTTP/SQL handoff."""

from copy import deepcopy
import json

import pytest
from qualification_service import service_journey
from qualification_timing import ACTIONS, read_ledger
from test_order_http_handoff import http_business as http_business  # noqa: F401

from sova.adapters.postgres_evidence_observations import PostgresEvidenceObservations
from sova.domain.contracts import ContractError

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


def test_real_timing_is_plan_bound_and_does_not_export_unreferenced_observation_bodies(postgres_database, http_business, tmp_path):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        admission = h.approve_and_admit(compiled, 'timing-approval')
        h.worker().drive(h.tenant, h.app, admission['unit_digest'])
        assert h.status(admission['unit_digest'])['state'] == 'HANDOFF_COMPLETE'
        PostgresEvidenceObservations(h.connect, tenant_id=h.tenant).record(
            h.app, 'unreferenced', {'probe': 'restore_drill', 'private_value': 'must-not-leave-the-database'})
        before = h.status(admission['unit_digest'])
        ledger = read_ledger(h.connect, h.tenant, h.app, admission)
        assert h.status(admission['unit_digest']) == before
        assert ledger['snapshot']['read_only'] and ledger['snapshot']['isolation'] == 'repeatable read'
        assert len(ledger['effects']) == 21 and {e['action'] for e in ledger['effects']} == set(ACTIONS)
        assert len(ledger['authority']) == 10 and ledger['measurement_receipts']
        assert ledger['scope']['operation_id'] == admission['operation_id']
        assert 'must-not-leave-the-database' not in json.dumps(ledger) and 'unreferenced' not in json.dumps(ledger)
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
