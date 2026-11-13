from dataclasses import FrozenInstanceError
import json
import subprocess
import sys

import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError
from sova.domain.service_contracts import MAX_CONTRACT_BYTES, ServiceContract
from sova.interfaces.cli import main


def test_contract_is_deterministic_immutable_and_never_an_approval():
    contract = example_service_contract()
    document = contract.document
    shuffled = dict(reversed(list(document.items())))
    assert ServiceContract.from_document(shuffled).fingerprint == contract.fingerprint
    document["spec"]["security"]["required_controls"].clear()
    assert contract.document["spec"]["security"]["required_controls"]
    with pytest.raises(FrozenInstanceError):
        contract.canonical = "{}"
    result = contract.inspect(expected_tenant="org-example")
    assert result["status"] == "NORMALIZED"
    assert (
        result["registered"] is result["executable"] is result["live_verified"] is False
    )
    assert result["blockers"]
    with pytest.raises(ContractError):
        contract.inspect(expected_tenant="other-tenant")


@pytest.mark.parametrize(
    "section",
    [
        "owner",
        "acknowledgment_semantics",
        "business",
        "migration",
        "data",
        "availability",
        "security",
        "residency",
        "operations",
        "approval",
        "exit",
    ],
)
def test_no_omitted_required_section_can_disappear_from_contract(section):
    data = example_service_contract().document
    del data["spec"][section]
    with pytest.raises(ContractError):
        ServiceContract.from_document(data)


@pytest.mark.parametrize("value", [True, False, 0, -1, "1", 1.5, 2**53])
def test_revision_is_an_exact_positive_safe_integer(value):
    data = example_service_contract().document
    data["metadata"]["revision"] = value
    with pytest.raises(ContractError):
        ServiceContract.from_document(data)


@pytest.mark.parametrize("value", [True, -1, 2**53, 0.1, "5000", None])
def test_pause_budget_cannot_be_coerced_or_rounded(value):
    data = example_service_contract().document
    data["spec"]["availability"]["max_write_admission_pause_ms"] = value
    with pytest.raises(ContractError):
        ServiceContract.from_document(data)


@pytest.mark.parametrize("value", [[], ["control", "control"], "control", [True]])
def test_required_controls_are_not_empty_duplicated_or_coerced(value):
    data = example_service_contract().document
    data["spec"]["security"]["required_controls"] = value
    with pytest.raises(ContractError):
        ServiceContract.from_document(data)


def test_unknown_fields_schema_and_duplicate_keys_are_rejected():
    for mutate in [
        lambda d: d.update(apiVersion="migration.platform/v99"),
        lambda d: d.update(kind="Approval"),
        lambda d: d["spec"].update(executable=True),
        lambda d: d["metadata"].update(approved=True),
        lambda d: d["spec"]["availability"].update(ignore_budget=True),
    ]:
        data = example_service_contract().document
        mutate(data)
        with pytest.raises(ContractError):
            ServiceContract.from_document(data)
    with pytest.raises(ContractError):
        ServiceContract('{"metadata":{},"metadata":{}}')


def test_partition_requires_explicit_boundary_and_no_cross_partition_writes():
    data = example_service_contract().document
    data["spec"]["migration"]["units"] = {
        "mode": "verified_partition",
        "partition_key": "customer_id",
        "partition_contract_ref": "partition-v1",
        "reject_cross_partition_transactions": True,
    }
    assert (
        ServiceContract.from_document(data).inspect(expected_tenant="org-example")[
            "executable"
        ]
        is False
    )
    data["spec"]["migration"]["units"]["reject_cross_partition_transactions"] = False
    with pytest.raises(ContractError):
        ServiceContract.from_document(data)
    del data["spec"]["migration"]["units"]["partition_contract_ref"]
    with pytest.raises(ContractError):
        ServiceContract.from_document(data)


def test_identity_extensions_survive_digest_but_are_not_silently_evaluated():
    original = example_service_contract()
    document = original.document
    document["spec"]["identity"] = {
        "remove_dependencies": ["source-cognito"],
        "refresh_token_portability_assumed": False,
    }
    contract = ServiceContract.from_document(document)
    assert contract.fingerprint != original.fingerprint
    assert contract.document["spec"]["identity"] == document["spec"]["identity"]
    assert contract.inspect(expected_tenant="org-example")[
        "unevaluated_extensions"
    ] == ["identity"]


def test_exit_cannot_remove_and_retain_the_same_dependency():
    document = example_service_contract().document
    document["spec"]["exit"]["allowed_remaining"] = ["source-db"]
    with pytest.raises(ContractError):
        ServiceContract.from_document(document)


def test_large_or_unsupported_json_cannot_bypass_the_schema():
    with pytest.raises(ContractError):
        ServiceContract(" " * (MAX_CONTRACT_BYTES + 1))
    with pytest.raises(ContractError):
        ServiceContract("[" * 30 + "0" + "]" * 30)
    with pytest.raises(ContractError):
        ServiceContract('{"x":NaN}')


def test_cli_normalizes_without_registering_and_redacts_failures(tmp_path, capsys):
    assert main(["service-contract-example"]) == 0
    path = tmp_path / "contract.json"
    path.write_text(capsys.readouterr().out)
    assert main(["service-contract-inspect", str(path), "--tenant", "org-example"]) == 0
    assert json.loads(capsys.readouterr().out)["registered"] is False
    assert (
        main(["service-contract-inspect", str(path), "--tenant", "other-tenant"]) == 2
    )
    capsys.readouterr()
    path.write_text('{"secret":"must-not-appear-in-output"}')
    assert main(["service-contract-inspect", str(path), "--tenant", "org-example"]) == 2
    output = capsys.readouterr()
    assert "must-not-appear-in-output" not in output.out + output.err


def test_real_python_module_entrypoint_produces_json_not_just_exit_zero(tmp_path):
    generated = subprocess.run(
        [sys.executable, "-m", "sova", "service-contract-example"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    value = json.loads(generated.stdout)
    assert value["kind"] == "ServiceContract"
    path = tmp_path / "contract.json"
    path.write_text(generated.stdout)
    inspected = subprocess.run(
        [
            sys.executable,
            "-m",
            "sova",
            "service-contract-inspect",
            str(path),
            "--tenant",
            "org-example",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    result = json.loads(inspected.stdout)
    assert result["service_id"] == "orders-service"
    assert result["registered"] is result["executable"] is False
