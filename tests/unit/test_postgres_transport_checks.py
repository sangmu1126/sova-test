"""The TLS checks of the read-only boundary and the write gate read the libpq
connection: a verified TLS connection proceeds, plain TCP is refused with a
contract error (never an attribute error that hides the reason)."""

from types import SimpleNamespace

from psycopg.pq import TransactionStatus
import pytest

from sova.adapters.postgres_readonly import begin_registered_read
from sova.domain.contracts import ContractError


class Stop(Exception):
    """Raised by the double once the transport check has passed."""


def connection(*, host="db.internal", ssl=True, sslmode="verify-full"):
    def execute(*_args, **_kwargs):
        raise Stop

    return SimpleNamespace(
        autocommit=False,
        info=SimpleNamespace(transaction_status=TransactionStatus.IDLE, host=host,
                             get_parameters=lambda: {"sslmode": sslmode}),
        pgconn=SimpleNamespace(ssl_in_use=ssl),
        execute=execute)


REGISTRATION = SimpleNamespace(allow_private_unix_fixture=False)


def test_verified_tls_passes_the_transport_check():
    with pytest.raises(Stop):  # reached the first statement after the check
        begin_registered_read(connection(), REGISTRATION)


@pytest.mark.parametrize("conn", [connection(ssl=False), connection(sslmode="require"),
                                  connection(host="/var/run/postgresql")])
def test_plain_unverified_or_unauthorized_socket_transport_is_refused(conn):
    with pytest.raises(ContractError):
        begin_registered_read(conn, REGISTRATION)


def test_the_write_gate_uses_the_same_libpq_attribute():
    from pathlib import Path

    import sova.adapters.postgres_write_gate as gate

    source = Path(gate.__file__).read_text()
    assert "pgconn.ssl_in_use" in source and "info.ssl_in_use" not in source
