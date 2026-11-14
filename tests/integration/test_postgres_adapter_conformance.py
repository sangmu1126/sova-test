"""The PostgreSQL offline-copy adapter against the provider-neutral conformance kit."""

from adapter_conformance import HandoffAdapterConformance
from postgres_world import PostgresWorld
import pytest

pytestmark = pytest.mark.postgres


class TestPostgresOfflineCopyConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, business):
        return PostgresWorld(business)
