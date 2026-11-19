"""The PostgreSQL offline-copy adapter against the provider-neutral conformance kit."""

from adapter_conformance import HandoffAdapterConformance
from adapter_conformance import apply, readback, SEQUENCES
from postgres_world import PostgresWorld
import pytest
from psycopg import sql

from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.postgres


class TestPostgresOfflineCopyConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, business):
        return PostgresWorld(business)

    def test_activation_refuses_nologin_even_when_database_grants_exist(self, world):
        for action in SEQUENCES["offline-copy"]:
            if action == "commit_route":
                break
            apply(world.adapter(), action)
        with world.b["connect"](world.names["tgt"]) as conn:
            conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(world.b["app"])))
        assert readback(world.adapter(), "activate_target") == EffectStatus.UNKNOWN
        assert not world.can_write("target", "app")
        # An observer must never silently make a cluster-wide role LOGIN.
        with world.b["connect"](world.names["tgt"]) as conn:
            assert conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname=%s",
                                (world.b["app"],)).fetchone() == (False,)
