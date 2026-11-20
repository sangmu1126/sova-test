import copy
from pathlib import Path
import importlib.util

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra/personal-journey"


def load(name, monkeypatch):
    monkeypatch.syspath_prepend(str(INFRA))
    spec = importlib.util.spec_from_file_location(
        "contract_renewal_test_" + name, INFRA / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_renewal_changes_only_revision_without_mutating_original(monkeypatch):
    from sova.application.service_contracts import example_service_contract

    module = load("renew_contract_attestation", monkeypatch)
    document = example_service_contract().document
    original = copy.deepcopy(document)
    metadata = document["metadata"]
    renewed = module.next_contract(document, metadata["tenant_id"], metadata["id"])
    assert document == original
    assert renewed.document["spec"] == original["spec"]
    assert renewed.document["metadata"] == {
        **original["metadata"],
        "revision": metadata["revision"] + 1,
    }
    assert renewed.fingerprint != example_service_contract().fingerprint


@pytest.mark.parametrize("changed", ["tenant", "application"])
def test_contract_renewal_refuses_other_scope(monkeypatch, changed):
    from sova.application.service_contracts import example_service_contract

    module = load("renew_contract_attestation", monkeypatch)
    document = example_service_contract().document
    with pytest.raises(ValueError, match="scope differs"):
        module.next_contract(
            document,
            "other" if changed == "tenant" else document["metadata"]["tenant_id"],
            "other" if changed == "application" else document["metadata"]["id"],
        )
