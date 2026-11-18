"""A source credential must never be reused for the destination writer probe."""

from copy import deepcopy

import pytest
from test_service_deployment import application

from sova.domain.contracts import ContractError
from sova.service.catalog import application_resolver


def configured(tmp_path):
    entry = application(tmp_path)
    mappings = {}
    for database in entry["databases"]:
        mappings[database] = {}
        for role in entry["writer_roles"]:
            file = tmp_path / (database + "-" + role)
            file.write_text(database + "-" + role + "-test-only")
            file.chmod(0o600)
            mappings[database][role] = str(file)
    entry["database_role_password_files"] = mappings
    return entry


def test_writer_connections_use_their_own_database_password_and_reread_rotation(tmp_path, monkeypatch):
    import psycopg

    captured = []
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kwargs: captured.append(kwargs))
    entry = configured(tmp_path)
    resolve, close = application_resolver(entry, route_for=lambda _: object())
    try:
        service = resolve("tenant-a", "orders")
        for database in ("src", "tgt"):
            service["connect_as"](database, "orders_app")
        assert [call["password"] for call in captured] == [
            "src-orders_app-test-only", "tgt-orders_app-test-only"]
        path = entry["database_role_password_files"]["tgt"]["orders_app"]
        with open(path, "w") as file:
            file.write("rotated-target-test-only")
        service["connect_as"]("tgt", "orders_app")
        assert captured[-1]["password"] == "rotated-target-test-only"
        with pytest.raises(ContractError, match="No credential"):
            service["connect_as"]("tgt", "unregistered")
        assert len(captured) == 3
    finally:
        close()


@pytest.mark.parametrize("fault", ("missing_database", "missing_writer", "unknown_writer", "mixed_legacy", "other_engine"))
def test_incomplete_credential_inventory_fails_before_route_or_database_access(tmp_path, fault):
    entry = deepcopy(configured(tmp_path))
    if fault == "missing_database":
        del entry["database_role_password_files"]["other"]
    elif fault == "missing_writer":
        del entry["database_role_password_files"]["src"]["orders_batch"]
    elif fault == "unknown_writer":
        entry["database_role_password_files"]["src"]["unregistered"] = "/private/untrusted"
    elif fault == "mixed_legacy":
        entry["role_password_files"] = {"orders_app": "/private/untrusted"}
    else:
        entry["engine"] = "mysql"

    def route(_):
        raise AssertionError("Invalid credential inventory must not reach a route provider")

    with pytest.raises(ContractError):
        application_resolver(entry, route_for=route)
