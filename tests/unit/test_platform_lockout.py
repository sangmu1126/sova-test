"""Platform lockout decisions without any service."""

import psycopg
import pytest

from sova.adapters import platform_lockout as lockout
from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.domain.contracts import ContractError


class Controls:
    def __init__(self, outcome):
        self.outcome = outcome

    def service(self, name):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {}


@pytest.mark.parametrize("outcome,expected", [
    (None, False), (KubernetesRequestRefused(401), True), (KubernetesRequestRefused(403), True),
    (KubernetesRequestRefused(500), None), (ContractError("transport"), None)])
def test_kubernetes_token(outcome, expected):
    controls = Controls(outcome)
    assert lockout.kubernetes_token(lambda: controls.service("x"))[0] is expected


@pytest.mark.parametrize("message,expected", [
    ('FATAL: role "sova_platform" is not permitted to log in', True),
    ('FATAL: role "sova_platform" does not exist', True),
    ("FATAL: password authentication failed for user", True),
    ('FATAL: database "orders" does not exist', None),
    ("connection refused", None)])
def test_postgres_role(message, expected):
    def connect():
        raise psycopg.OperationalError(message)
    assert lockout.postgres_role(connect)[0] is expected


class Http:
    def __init__(self, status=None):
        self.status = status

    def post(self, url, data):
        if self.status is None:
            raise ConnectionError("unreachable")
        return type("R", (), {"status_code": self.status})()


@pytest.mark.parametrize("status,expected", [(200, False), (401, True), (400, True),
                                             (503, None), (None, None)])
def test_oidc_admin(status, expected):
    assert lockout.oidc_admin(Http(status), "u", username="a", password="p")[0] is expected


def test_one_undecided_lockout_withholds_the_pass():
    ok = lambda: (True, {})  # noqa: E731
    undecided = lambda: (None, {})  # noqa: E731
    assert lockout.platform_exit({"a": ok, "b": ok}, {"deploy": ok})[0]
    assert not lockout.platform_exit({"a": ok, "b": undecided}, {"deploy": ok})[0]
    assert not lockout.platform_exit({"a": ok}, {"deploy": lambda: (False, {})})[0]
    with pytest.raises(ContractError):
        lockout.platform_exit({}, {"deploy": ok})
