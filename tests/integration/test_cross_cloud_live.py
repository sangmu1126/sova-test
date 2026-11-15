"""A handoff between two clouds: RDS (AWS) to Cloud SQL (GCP), and back the
other way, through the same reference adapter, conformance kit and control
plane as every other PostgreSQL path.

Both ends are managed servers whose master users are not superusers, both
accept encrypted connections only, and both certificates are verified, so
the contract keeps its transport control instead of dropping it as the local
reference profile does. The copy runs through this process, across the
internet between the two providers' Seoul regions.
"""

from datetime import UTC, datetime, timedelta

from adapter_conformance import HandoffAdapterConformance
from handoff_drive import run_handoff
from postgres_business import TABLES
from postgres_world import PostgresWorld
import pytest

from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.aws_live, pytest.mark.gcp_live, pytest.mark.postgres]


class TestCrossCloudConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, cross_cloud_business):
        return PostgresWorld(cross_cloud_business)


def managed_profile(data):
    """Transport control kept (both ends verified TLS); budgets for an internet path."""
    availability = data["spec"]["availability"]
    availability["max_write_admission_pause_ms"] = 60000
    availability["max_client_completion_wait_ms"] = 120000


def test_the_control_plane_hands_off_between_clouds_with_transport_verified(
        postgres_database, cross_cloud_business):
    b = cross_cloud_business
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), b,
                    adjust=managed_profile)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["record"].held_from, x["observed"].get("uncertain"),
        x["observed"].get("failed"))
    assert x["route"].get() == b["names"]["tgt"]
    for table, _ in TABLES:
        assert x["probes"].content_equal(table)[0], table
    assert x["probes"].order_effects_once()[0]
    assert x["probes"].write_denied("source")[0]
    for side in ("source", "target"):  # the obligation measured the target; both are
        verified, observation = x["probes"].transport_encrypted(side)
        assert verified, (side, observation)
    observed = x["observed"]
    pause = (observed["activate_target"] - observed["pause_admission"]).total_seconds()
    print(f"measured write-admission pause {b['direction']}: {pause * 1000:.0f} ms")
    with b["connect_as"](b["names"]["tgt"], b["app"]) as conn:  # business continues
        conn.execute("INSERT INTO orders VALUES (51, 'customer-1', 'key-51', 510)")
