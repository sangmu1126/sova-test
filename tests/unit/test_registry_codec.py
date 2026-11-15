"""Every registry kind decodes to its typed value and back to the same document."""

from contract_world import build_world
import pytest

from sova.adapters.registration import registry_codec as codecs
from sova.domain.contracts import ContractError


@pytest.fixture
def values(now):
    w = build_world(now)
    return {"service_contract": w["contract"], "dependency_snapshot": w["snapshot"],
            "environment_profile": w["source"], "capability_manifest": w["manifest"],
            "strategy_qualification": w["qualification"]}


@pytest.mark.parametrize("kind", codecs.KINDS)
def test_documents_round_trip_to_the_same_fingerprint(values, kind):
    value = values[kind]
    document = codecs.encode(value)
    assert codecs.kind_of(value) == kind
    decoded = codecs.decode(kind, document)
    assert decoded.fingerprint == value.fingerprint
    assert codecs.encode(decoded) == document


@pytest.mark.parametrize("kind", ["environment_profile", "capability_manifest",
                                  "strategy_qualification"])
def test_unknown_or_missing_fields_are_refused(values, kind):
    document = codecs.encode(values[kind])
    with pytest.raises(ContractError):
        codecs.decode(kind, {**document, "unexpected": 1})
    trimmed = dict(document)
    trimmed.pop("schema_version")
    with pytest.raises(ContractError):
        codecs.decode(kind, trimmed)


def test_only_the_normalized_form_is_accepted(values):
    document = codecs.encode(values["environment_profile"])
    shuffled = {**document, "versions": dict(reversed(list(document["versions"].items())))}
    # Same facts, but a document is stored exactly as the compiler will read it.
    assert codecs.decode("environment_profile", shuffled).fingerprint == \
        values["environment_profile"].fingerprint
    qualification = codecs.encode(values["strategy_qualification"])
    with pytest.raises(ContractError, match="normalized"):
        codecs.decode("strategy_qualification",
                      {**qualification, "issued_at": qualification["issued_at"].replace(
                          "+00:00", "Z")})


def test_subjects_and_tenants(values):
    assert codecs.subject_of(values["capability_manifest"]) == "pg-offline-copy.offline-copy"
    assert codecs.tenant_of(values["environment_profile"]) is None
    assert codecs.tenant_of(values["service_contract"]) == values["service_contract"].tenant_id
