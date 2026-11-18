"""A read is tried again after the connection itself failed before any answer
(each attempt with its own deadline); a write never is, and a refusal, a bad
answer or an answer cut short is final."""

import http.client
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


class Connection:
    """An HTTPS connection double: ``answer`` is what ``getresponse`` gives
    (an exception is raised), and the answer's body read fails with ``cut``."""

    def __init__(self, answer, cut=None):
        self.answer, self.cut = answer, cut
        self.sock = SimpleNamespace(shutdown=lambda how: None)

    def __call__(self, host, port, timeout, context):
        return self

    def connect(self):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        cut = self.cut

        def read(n):
            raise cut

        return SimpleNamespace(status=200, getheader=lambda name, default: default,
                               read=read)

    def close(self):
        pass


def one_attempt(monkeypatch, connection):
    c = object.__new__(KubernetesHTTPS)
    c.context = None
    reg = SimpleNamespace(endpoint="https://api.test:443", approved_addresses=("10.0.0.1",))
    monkeypatch.setattr(https.http.client, "HTTPSConnection", connection)
    return lambda: c._attempt(reg, "token", "GET", "/api/v1/namespaces/a/pods", {},
                              timeout=5, body=None, content_type=None, accept=(200,),
                              limit=1024, raw=False, headers=None)


def test_no_answer_is_a_failure_of_the_path(monkeypatch):
    attempt = one_attempt(monkeypatch, Connection(http.client.RemoteDisconnected("closed")))
    with pytest.raises(KubernetesTransportFailure):
        attempt()


def test_an_answer_cut_short_is_final(monkeypatch):
    attempt = one_attempt(monkeypatch, Connection(None, cut=http.client.IncompleteRead(b" ")))
    with pytest.raises(ContractError) as failed:
        attempt()
    assert not isinstance(failed.value, KubernetesTransportFailure)
