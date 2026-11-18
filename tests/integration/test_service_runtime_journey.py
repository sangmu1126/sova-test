"""Real service CLI processes and HTTPS JWKS; no deployed worker/cloud claim."""

import pytest
from qualification_runtime import runtime_service_journey
import test_order_service_journey as journey
from test_order_http_handoff import http_business as http_business  # noqa: F401

pytestmark = pytest.mark.postgres


def test_runtime_api_restart_preserves_forward_and_failback_approvals(
        postgres_database, http_business, tmp_path, monkeypatch):
    # Reuse the same business and safety assertions rather than an easier
    # runtime-only script. RuntimeJourney restarts the API after each approval.
    monkeypatch.setattr(journey, "service_journey", runtime_service_journey)
    journey.test_service_orders_survive_target_writes_and_separately_approved_failback(
        postgres_database, http_business, tmp_path)


def test_untrusted_jwks_ca_denies_new_identity_until_runtime_configuration_is_fixed(
        postgres_database, http_business, tmp_path):
    with runtime_service_journey(postgres_database, http_business, tmp_path,
                                  case_name="untrusted-jwks-ca", wrong_jwks_ca=True) as h:
        path = f"{h.base}/registrations/service_contract/{h.app}"
        response = h.call("GET", path)
        assert response.status_code == 503, response.text
        assert response.json() == {"error": "identity provider unavailable"}
        assert h.jwks.gets == 0  # TLS failed before HTTP, no injected JWKS callback
        assert h.plane._registrations(h.tenant).current("service_contract", h.app) is None
        h.environment["SSL_CERT_FILE"] = str(h.jwks.ca)
        h.restart_api()
        response = h.call("GET", path)
        assert response.status_code == 404, response.text  # authenticated; no record
        assert h.jwks.gets > 0
        h.record("untrusted-jwks-ca", denied_status=503, authenticated_after_fix_status=404)


def test_wrong_database_ca_fails_readiness_and_recovers_only_after_corrected_restart(
        postgres_database, http_business, tmp_path):
    with runtime_service_journey(postgres_database, http_business, tmp_path,
                                  case_name="untrusted-database-ca", wrong_database_ca=True) as h:
        assert h.client.get("/healthz").status_code == 200
        response = h.client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not ready"}
        h.dsn_file.write_text(h.runtime_dsn)
        h.restart_api()
        assert h.client.get("/readyz").status_code == 200
        assert h.plane._registrations(h.tenant).current("service_contract", h.app) is None
        h.record("untrusted-database-ca", readiness_before=503, readiness_after=200,
                 configuration_fix="run-owned CA reference corrected, then API restarted")
