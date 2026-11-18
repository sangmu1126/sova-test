"""A read is tried again after the connection itself failed (each attempt with
its own deadline); a write never is, and a refusal or a bad answer is final."""

from types import SimpleNamespace

import pytest

from sova.adapters.kubernetes import https
from sova.adapters.kubernetes.https import (
    KubernetesHTTPS,
    KubernetesRequestRefused,
    KubernetesTransportFailure,
)
from sova.domain.contracts import ContractError


def client(outcomes, monkeypatch):
    """A transport whose attempts answer ``outcomes`` in turn (an exception is raised)."""
    c = object.__new__(KubernetesHTTPS)
    c.registration = SimpleNamespace(endpoint="https://api.test:443")
    c.token_provider = lambda: "token"
    calls = []

    def attempt(reg, token, method, path, query, **kwargs):
        calls.append(method)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    c._attempt = attempt
    monkeypatch.setattr(https.time, "sleep", lambda seconds: None)
    return c, calls


def test_a_read_survives_a_dropped_connection(monkeypatch):
    c, calls = client([KubernetesTransportFailure("x"), KubernetesTransportFailure("y"),
                       {"ok": True}], monkeypatch)
    assert c._request("GET", "/api/v1/namespaces/a/pods", {}, timeout=5) == {"ok": True}
    assert calls == ["GET"] * 3


def test_a_read_gives_up_after_its_attempts(monkeypatch):
    c, calls = client([KubernetesTransportFailure(str(i)) for i in range(5)], monkeypatch)
    with pytest.raises(KubernetesTransportFailure):
        c._request("GET", "/api/v1/namespaces/a/pods", {}, timeout=5)
    assert len(calls) == https._READ_ATTEMPTS


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_a_write_is_never_repeated(monkeypatch, method):
    c, calls = client([KubernetesTransportFailure("x"), {"ok": True}], monkeypatch)
    with pytest.raises(KubernetesTransportFailure):
        c._request(method, "/api/v1/namespaces/a/pods", {}, timeout=5, body=b"{}",
                   content_type="application/json")
    assert calls == [method]


@pytest.mark.parametrize("final", [KubernetesRequestRefused(403), ContractError("bad answer")])
def test_a_refusal_or_a_bad_answer_is_not_retried(monkeypatch, final):
    c, calls = client([final, {"ok": True}], monkeypatch)
    with pytest.raises(ContractError):
        c._request("GET", "/api/v1/namespaces/a/pods", {}, timeout=5)
    assert len(calls) == 1
