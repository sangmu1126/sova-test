from support import IngressAdapter
from datetime import UTC, datetime, timedelta

import pytest

from sova.domain.contracts import (
    EffectCommand,
    ResourceScope,
    canonical_json,
)


@pytest.fixture
def now(request):
    if request.node.get_closest_marker("postgres"):
        # The database rejects times ahead of its own clock, and a container's
        # clock can trail the host's by a millisecond or more; a value taken on
        # the host and written at once would then be "in the future".
        return datetime.now(UTC) - timedelta(seconds=1)
    return datetime(2026, 10, 31, 7, 0, tzinfo=UTC)


@pytest.fixture
def scope():
    return ResourceScope("tenant-a", "app-a", "environment-a", "owned-ingress")


@pytest.fixture
def command(scope, now):
    return EffectCommand(
        scope,
        "operation-1",
        "isolate",
        "test-ingress",
        "set_ingress",
        "operation-1:isolate",
        "a" * 64,
        "revision-1",
        1,
        now + timedelta(minutes=10),
        canonical_json({"blocked": True}),
    )


@pytest.fixture
def adapter():
    return IngressAdapter()


pytest_plugins = ["postgres_fixtures", "postgres_business", "s3_fixtures", "keycloak_fixtures", "aws_fixtures", "gcp_fixtures", "mysql_business", "kafka_business", "rabbitmq_business", "volume_business", "session_business", "gateway_business"]


def _verified_postgres_transport(dsn):
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(dsn)
    return values.get("sslmode") == "verify-full" or values.get("host", "").startswith("/")


def pytest_collection_modifyitems(config, items):
    """Tests of the read and write gates need a server they will accept:
    certificate- and hostname-verified TLS, or a private Unix socket. Against
    a plain TCP server they are skipped with the way to run them, never
    reported as product failures."""
    import os

    dsn = os.environ.get("SOVA_TEST_POSTGRES_DSN")
    if not dsn or _verified_postgres_transport(dsn):
        return
    skip = pytest.mark.skip(reason="verified-TLS PostgreSQL required: run with "
                                   "SOVA_TEST_POSTGRES_DSN=$SOVA_TEST_POSTGRES_TLS_DSN "
                                   "-m postgres_tls")
    for item in items:
        if "postgres_tls" in item.keywords:
            item.add_marker(skip)
