"""A matching database comment never proves another resource's residence."""

import pytest

from sova.service.postgres_execution import data_measure


class MisleadingDatabaseLabel:
    def zone(self, *_):
        pytest.fail("An inventory label must not substitute for placement evidence")


def obligation(category):
    return {"id": "residency." + category, "kind": "placement_evidence",
            "category": category, "allowed_zones": ["approved-vm"]}


@pytest.mark.parametrize("category", ["business_data", "backups", "audit", "ai_inputs"])
def test_unconfigured_observer_blocks_even_when_database_label_matches(category):
    measure = data_measure(MisleadingDatabaseLabel(), other_db=None, scratch_db=None)
    passed, fact = measure(obligation(category), {})
    assert passed is False
    assert fact == {"probe": "placement", "category": category,
                    "reason": "category_observer_unavailable"}


def test_business_data_success_does_not_satisfy_audit_or_backups():
    observed = []

    def observer(category, allowed):
        observed.append((category, allowed))
        return category == "business_data", {"category": category}

    measure = data_measure(MisleadingDatabaseLabel(), other_db=None, scratch_db=None,
                           placement=observer)
    assert measure(obligation("business_data"), {})[0] is True
    assert measure(obligation("audit"), {})[0] is False
    assert measure(obligation("backups"), {})[0] is False
    assert observed == [(c, frozenset({"approved-vm"}))
                        for c in ("business_data", "audit", "backups")]


@pytest.mark.parametrize("bad", [
    {"id": "residency.audit", "category": "business_data"},
    {"id": "residency.audit", "category": "unknown"},
    {"id": "residency.audit"},
])
def test_invalid_or_legacy_category_cannot_call_observer(bad):
    def observer(*_):
        pytest.fail("Malformed category must not reach a provider observer")

    measure = data_measure(MisleadingDatabaseLabel(), other_db=None, scratch_db=None,
                           placement=observer)
    passed, fact = measure({**obligation("audit"), "category": None, **bad}, {})
    assert passed is False
    assert fact["reason"] == "category_binding_invalid"
