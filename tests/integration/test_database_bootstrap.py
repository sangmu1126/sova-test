"""Real TLS DB metadata transfer, with explicitly injected later failures."""

from dataclasses import replace

import psycopg
import pytest
from qualification_service import ServiceJourney, observe_database_versions

pytestmark = pytest.mark.postgres


def test_observed_versions_are_not_requeried_during_later_setup(postgres_database, business, tmp_path):
    observation = observe_database_versions(business)
    assert observation.source_tls and observation.target_tls
    calls = []
    def unavailable(database):
        calls.append(database)
        raise psycopg.errors.ConnectionTimeout("Injected after actual version discovery")
    blocked = {**business, "connect": unavailable}
    journey = ServiceJourney(postgres_database, blocked, tmp_path, observed_versions=observation)
    assert set(journey.database_versions.values()) == {observation.source_version, observation.target_version}
    assert calls == []
    # Transferring metadata grants no live connection or outage bypass.
    with pytest.raises(psycopg.errors.ConnectionTimeout):
        journey.business["connect"](business["names"]["src"])
    assert calls == [business["names"]["src"]]
    with journey.connect() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_version_discovery_timeout_is_not_retried_or_reported_as_success(postgres_database, business, tmp_path):
    calls = []
    def unavailable(database):
        calls.append(database)
        raise psycopg.errors.ConnectionTimeout("Injected first discovery failure")
    with pytest.raises(psycopg.errors.ConnectionTimeout):
        ServiceJourney(postgres_database, {**business, "connect": unavailable}, tmp_path)
    assert calls == [business["names"]["src"]]


def test_another_database_observation_is_rejected(postgres_database, business, tmp_path):
    observation = observe_database_versions(business)
    with pytest.raises(ValueError, match="scope"):
        ServiceJourney(postgres_database, business, tmp_path,
                       observed_versions=replace(observation, target_database="different-fixture"))
    assert observation.document["qualification_issued"] is False
    assert observation.document["authorizes_execution"] is False
