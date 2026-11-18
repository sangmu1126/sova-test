"""Accepted configuration boundaries round-trip through a real SQL lease."""

from datetime import timedelta

import pytest
from qualification_service import service_journey
from test_order_http_handoff import http_business as http_business  # noqa: F401

from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.service.config import WorkerConfig

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("seconds", [10, 120, 300])
def test_accepted_config_can_acquire_and_release_the_exact_duration_in_postgres(
        postgres_database, http_business, tmp_path, seconds):
    key = tmp_path / "fixture-key"
    key.write_text("11" * 32)
    key.chmod(0o600)
    execution = tmp_path / "execution.json"
    execution.write_text('{"version":1}')
    config = WorkerConfig.from_env({
        "SOVA_TENANTS": "tenant-startup", "SOVA_EVIDENCE_KEY_FILE": str(key),
        "SOVA_EVIDENCE_KEY_ID": "fixture-key", "SOVA_EXECUTION_FILE": str(execution),
        "SOVA_LEASE_SECONDS": str(seconds), "POD_NAME": "startup-fixture",
        "POD_UID": "11111111-1111-1111-1111-111111111111", "POD_NAMESPACE": "fixture"})
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        h.approve_and_admit(compiled, "startup-approval")
        repo = PostgresLifecycleRepository(h.connect, tenant_id=h.tenant)
        ownership = PostgresExecutionOwnership(repo)
        operation = "handoff-" + compiled["plan_id"][-24:]
        with repo.transaction() as conn:
            before = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        lease = ownership.acquire(h.app, operation, "startup-qualification",
                                   lease_seconds=config.lease_seconds)
        try:
            with repo.transaction() as conn:
                until, after = conn.execute(
                    "SELECT valid_until,clock_timestamp() FROM sova_control.app_execution "
                    "WHERE application_id=%s", (h.app,)).fetchone()
            assert before + timedelta(seconds=seconds) <= until <= after + timedelta(seconds=seconds)
        finally:
            ownership.release(lease)
        with repo.transaction() as conn:
            owner, valid_until = conn.execute("SELECT owner_id,valid_until FROM sova_control.app_execution "
                                              "WHERE application_id=%s", (h.app,)).fetchone()
        assert owner is None and valid_until is None
        h.record("startup-lease-" + str(seconds), configured_seconds=config.lease_seconds,
                 database_before=before.isoformat(), database_valid_until=until.isoformat(),
                 database_after=after.isoformat(), lease_released=True,
                 business_effect_executed=False, worker_cli_deployed=False)
