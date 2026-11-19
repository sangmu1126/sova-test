"""Fresh test PKI cannot silently outlive its admitted exercise window."""
from datetime import UTC, datetime, timedelta

from cryptography import x509
import pytest

from qualification_postgres import certificates


def test_explicit_window_bounds_every_generated_test_certificate(tmp_path):
    end = (datetime.now(UTC) + timedelta(minutes=8)).replace(microsecond=0)
    certificates(tmp_path, valid_until=end)
    for name in ('ca.pem', 'client.pem', 'server/server.pem'):
        assert x509.load_pem_x509_certificate((tmp_path / name).read_bytes()).not_valid_after_utc == end


@pytest.mark.parametrize('deadline', [
    datetime.now(UTC) - timedelta(seconds=1),
    datetime.now(UTC) + timedelta(days=2),
    datetime.now().replace(tzinfo=None) + timedelta(minutes=8),
])
def test_invalid_window_fails_before_any_key_is_written(tmp_path, deadline):
    with pytest.raises(ValueError, match='deadline'):
        certificates(tmp_path, valid_until=deadline)
    assert not list(tmp_path.iterdir())
