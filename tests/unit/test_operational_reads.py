from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sova.domain.contracts import ContractError
from sova.service.operational_reads import OperationalReadModels
from sova.service.control_plane import (
    ControlPlane,
    Principal,
    Role,
    Forbidden,
    NotFound,
)
from sova.service.api import create_app

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def fixture(tmp_path):
    path = tmp_path / "health.json"
    data = dict(
        passed=True,
        tenant_id="tenant",
        application_id="orders",
        gateway_source_sha256="a" * 64,
        completed_at=NOW.isoformat(),
        valid_until=(NOW + timedelta(minutes=5)).isoformat(),
        application_server_instrumented=False,
        slo_qualified=False,
        actual_http_receipts=[
            dict(
                side="source",
                status=200,
                duration_ms=12.5,
                verified_application_https=True,
            )
        ],
    )
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    entry = dict(
        tenant_id="tenant",
        application_id="orders",
        health=dict(file=str(path), producer_sha256="a" * 64),
        cost=None,
    )
    return path, data, entry


def test_actual_fields_are_projected_without_paths_or_authority(tmp_path):
    path, data, entry = fixture(tmp_path)
    data["secret"] = "do-not-display"
    path.write_text(json.dumps(data))
    frame = OperationalReadModels([entry], clock=lambda: NOW).read("tenant", "orders")
    assert frame["health"]["state"] == "fresh"
    assert frame["health"]["samples"][0]["status"] == 200
    assert frame["authorizes_execution"] is False
    assert str(tmp_path) not in json.dumps(
        frame
    ) and "do-not-display" not in json.dumps(frame)
    assert (
        OperationalReadModels([entry], clock=lambda: NOW + timedelta(minutes=5)).read(
            "tenant", "orders"
        )["health"]["state"]
        == "historical"
    )
    assert (
        OperationalReadModels([entry], clock=lambda: NOW).read("tenant", "other")[
            "health"
        ]["state"]
        == "unavailable"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "other"},
        {"passed": False},
        {"application_id": "other"},
        {"gateway_source_sha256": "b" * 64},
        {"slo_qualified": True},
        {"application_server_instrumented": True},
        {"actual_http_receipts": []},
        {
            "actual_http_receipts": [
                dict(
                    side="source",
                    status=True,
                    duration_ms=1,
                    verified_application_https=True,
                )
            ]
        },
        {
            "actual_http_receipts": [
                dict(
                    side="source",
                    status=200,
                    duration_ms=float("inf"),
                    verified_application_https=True,
                )
            ]
        },
        {"completed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"valid_until": (NOW + timedelta(minutes=6)).isoformat()},
    ],
)
def test_invalid_measurement_never_becomes_fresh(tmp_path, change):
    path, data, entry = fixture(tmp_path)
    data.update(change)
    path.write_text(json.dumps(data))
    assert (
        OperationalReadModels([entry], clock=lambda: NOW).read("tenant", "orders")[
            "health"
        ]["state"]
        == "unavailable"
    )


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "public", "malformed", "duplicate"]
)
def test_unreadable_private_file_is_unknown(tmp_path, kind):
    path, data, entry = fixture(tmp_path)
    if kind == "missing":
        path.unlink()
    if kind == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    if kind == "public":
        path.chmod(0o644)
    if kind == "malformed":
        path.write_text("[]")
    if kind == "duplicate":
        path.write_text('{"tenant_id":"tenant","tenant_id":"other"}')
    assert (
        OperationalReadModels([entry], clock=lambda: NOW).read("tenant", "orders")[
            "health"
        ]["state"]
        == "unavailable"
    )


def test_duplicate_operator_binding_refused(tmp_path):
    _, _, entry = fixture(tmp_path)
    with pytest.raises(ContractError):
        OperationalReadModels([entry, entry])


@pytest.mark.parametrize(
    "amount,estimated,available",
    [("0", True, True), ("NaN", False, True), ("1", False, False)],
)
def test_cost_refuses_unverified_zero_nonfinite_or_inconsistent(
    tmp_path, amount, estimated, available
):
    path, _, entry = fixture(tmp_path)
    path.write_text(
        json.dumps(
            dict(
                account_id="265233844540",
                run_id="b0be270e62",
                complete_tco=False,
                comparison_authorized=False,
                available=available,
                amount=amount,
                currency="USD",
                estimated=estimated,
                observed_at=NOW.isoformat(),
                reason=None,
            )
        )
    )
    entry["health"] = None
    entry["cost"] = dict(file=str(path), account_id="265233844540", run_id="b0be270e62")
    assert (
        OperationalReadModels([entry], clock=lambda: NOW).read("tenant", "orders")[
            "cost"
        ]["state"]
        == "unavailable"
    )


def test_authentication_role_app_guard_before_reader():
    calls = []
    plane = ControlPlane(lambda: None, settings=None, clock=lambda: NOW)
    plane._registrations = lambda tenant: SimpleNamespace(
        current=lambda kind, app: object() if app == "orders" else None
    )
    reader = SimpleNamespace(
        read=lambda *scope: calls.append(scope) or {"authorizes_execution": False}
    )
    roleless = Principal("who", "tenant", frozenset())
    with pytest.raises(Forbidden):
        plane.operational_observations(roleless, "orders", reader)
    auditor = Principal("who", "tenant", frozenset({Role.AUDITOR}))
    with pytest.raises(NotFound):
        plane.operational_observations(auditor, "missing", reader)
    assert not calls
    client = TestClient(create_app(plane, lambda _: auditor, operational_reads=reader))
    assert (
        client.get(
            "/v1/tenants/other/applications/orders/operational-observations"
        ).status_code
        == 403
    )
    assert not calls
    assert (
        client.get(
            "/v1/tenants/tenant/applications/orders/operational-observations"
        ).status_code
        == 200
    )
    assert calls == [("tenant", "orders")]


@pytest.mark.parametrize(
    "available,amount,currency,estimated,reason",
    [
        (False, None, None, True, "estimated_zero_not_verified"),
        (True, "0.27", "USD", True, None),
        (True, "0", "USD", False, None),
    ],
)
def test_bounded_billing_keeps_unknown_and_verified_amounts_separate(
    tmp_path, available, amount, currency, estimated, reason
):
    path, _, entry = fixture(tmp_path)
    path.write_text(
        json.dumps(
            dict(
                account_id="265233844540",
                run_id="b0be270e62",
                complete_tco=False,
                comparison_authorized=False,
                available=available,
                amount=amount,
                currency=currency,
                estimated=estimated,
                observed_at=NOW.isoformat(),
                reason=reason,
            )
        )
    )
    entry["health"] = None
    entry["cost"] = dict(file=str(path), account_id="265233844540", run_id="b0be270e62")
    result = OperationalReadModels([entry], clock=lambda: NOW).read("tenant", "orders")[
        "cost"
    ]
    assert result["state"] == "fresh"
    assert (result["available"], result["amount"]) == (available, amount)
    assert result["complete_tco"] is False and result["comparison_authorized"] is False
