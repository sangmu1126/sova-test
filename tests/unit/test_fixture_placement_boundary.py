"""Declared local inventory must stay test-only and cannot imply an allowed location."""

from contextlib import contextmanager
import pytest
from qualification_fixture_placement import FixturePlacement


@contextmanager
def database(identity=("sova-persistence-test", "sova_test")):
    class Result:
        def fetchone(self):
            return identity
    class Connection:
        def execute(self, _):
            return Result()
    yield Connection()


@pytest.mark.parametrize("identity", [("production", "sova_test"),
                                       ("sova-persistence-test", "customer"), (None, None)])
def test_fixture_inventory_refuses_a_non_test_control_database(identity):
    with pytest.raises(ValueError, match="isolated test control database"):
        FixturePlacement("tenant", "app", control_connect=lambda: database(identity))


def test_unknown_category_and_non_allowed_zone_fail_without_inventing_location():
    observer = FixturePlacement("tenant", "app", control_connect=database)
    assert observer("audit", frozenset({"somewhere-else"}))[0] is False
    assert observer("ai_inputs", frozenset({"approved-zone-a"}))[0] is False
    passed, fact = observer("audit", frozenset({"approved-zone-a"}))
    assert passed and fact["fixture_only"] is True
    assert fact["operational_qualification"] is False
    assert (fact["tenant_id"], fact["application_id"]) == ("tenant", "app")
