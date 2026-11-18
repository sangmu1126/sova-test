"""Mount/config rejection tests; these do not claim a real TLS handshake."""

from datetime import UTC, datetime, timedelta
import json

from psycopg.conninfo import make_conninfo
import pytest

from sova.qualification.order_runtime import load_configuration


@pytest.fixture
def mounted(tmp_path):
    config = {"version": 1, "tenant_id": "tenant-local", "application_id": "orders-service",
        "run_id": "canary-run", "side": "source", "deployment_digest": "sha256:" + "a" * 64,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    dsn = {"host": "source-db", "dbname": "source_orders", "user": "orders_app",
        "password": "unit-test-placeholder", "sslmode": "verify-full",
        "sslrootcert": str(tmp_path / "database-ca.pem"), "connect_timeout": 2}

    def write(**changes):
        contents = {"config.json": json.dumps(config), "database.dsn": make_conninfo(**{**dsn, **changes}),
                    "client.token": "unit-test-only-" + "a" * 40, "database-ca.pem": "unit-test CA placeholder"}
        for name, value in contents.items():
            path = tmp_path / name
            path.write_text(value)
            path.chmod(0o600)
        return tmp_path

    return tmp_path, config, dsn, write


def test_fixed_mount_uses_one_non_admin_database_without_contacting_it(mounted):
    root, _, _, write = mounted
    write()
    value = load_configuration(root)
    assert (value.role, value.database, value.side) == ("orders_app", "source_orders", "source")
    assert value.image_digest == "sha256:" + "a" * 64


@pytest.mark.parametrize("changes", [
    {"sslmode": "require"}, {"sslmode": "disable"}, {"user": "postgres"},
    {"host": "source-db,other-db"}, {"hostaddr": "127.0.0.1,192.0.2.1"},
    {"password": ""}, {"connect_timeout": 0}, {"connect_timeout": 30},
    {"sslrootcert": "/tmp/unregistered-ca.pem"}, {"passfile": "/tmp/passwords"},
])
def test_credentials_cannot_enable_tls_downgrade_admin_or_fallback(mounted, changes):
    root, _, _, write = mounted
    write(**changes)
    with pytest.raises(ValueError):
        load_configuration(root)


def test_world_readable_client_credential_is_rejected(mounted):
    root, _, _, write = mounted
    write()
    (root / "client.token").chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        load_configuration(root)


def test_secret_projection_may_resolve_inside_mount_but_not_outside(mounted):
    root, _, _, write = mounted
    write()
    token = root / "client.token"
    nested = root / "data"
    nested.mkdir()
    token.rename(nested / "token")
    token.symlink_to(nested / "token")
    assert load_configuration(root).run == "canary-run"
    token.unlink()
    token.symlink_to(root.parent / "unregistered-secret")
    with pytest.raises(ValueError, match="escaped"):
        load_configuration(root)


def test_expired_registration_does_not_become_a_live_canary(mounted):
    root, config, _, write = mounted
    config["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    write()
    with pytest.raises(ValueError, match="deadline"):
        load_configuration(root)
