"""The API server's WebSocket port-forward, spoken by the pinned client: an
upgrade that must answer with this key and protocol, the port's two channels,
masked client frames, and an answer read until the Pod closes - against a
scripted server."""

import base64
import hashlib
import struct
from types import SimpleNamespace

import pytest

from sova.adapters.kubernetes.https import KubernetesRequestRefused, _masked, ws_frame
from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls
from sova.domain.contracts import ContractError

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(payload, opcode=2):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def client_frames(data):
    """The (opcode, unmasked payload) of every client frame in ``data``."""
    out = []
    while data:
        opcode, length = data[0] & 0x0F, data[1] & 0x7F
        at = 2
        if length == 126:
            length, at = struct.unpack(">H", data[2:4])[0], 4
        elif length == 127:
            length, at = struct.unpack(">Q", data[2:10])[0], 10
        assert data[1] & 0x80, "a client frame must be masked"
        key = data[at:at + 4]
        out.append((opcode, _masked(data[at + 4:at + 4 + length], key)))
        data = data[at + 4 + length:]
    return out


class Peer:
    """A scripted API server: answers the upgrade, opens the port's channels,
    then sends ``answer`` on channel 0 (and ``error`` on 1) and closes."""

    def __init__(self, answer=b"", *, status=101, accept=None, protocol="v4.channel.k8s.io",
                 error=b"", closes=True):
        self.sent, self.pending = b"", b""
        self.answer, self.status, self.accept = answer, status, accept
        self.protocol, self.error, self.upgraded = protocol, error, False
        self.closes = closes

    def sendall(self, data):
        self.sent += data
        if not self.upgraded and b"\r\n\r\n" in self.sent:
            self.upgraded = True
            key = [line.split(b": ", 1)[1] for line in self.sent.split(b"\r\n")
                   if line.startswith(b"Sec-WebSocket-Key")][0]
            accept = self.accept or base64.b64encode(hashlib.sha1(key + GUID).digest())
            self.pending += (f"HTTP/1.1 {self.status} X\r\nSec-WebSocket-Accept: "
                             f"{accept.decode()}\r\nSec-WebSocket-Protocol: {self.protocol}"
                             "\r\n\r\n").encode()
            if self.status == 101:
                self.pending += server_frame(b"\x00" + struct.pack("<H", 8080))
                self.pending += server_frame(b"\x01" + struct.pack("<H", 8080))
        elif self.upgraded and data:
            self.pending += server_frame(b"\x00" + self.answer[:70000]) + server_frame(
                b"\x00" + self.answer[70000:]) + (server_frame(b"\x01" + self.error)
                                                  if self.error else b"")
            if self.closes:
                self.pending += server_frame(b"\x03\xe8", opcode=8)

    def recv(self, n):
        if not self.pending and not self.closes:
            raise AssertionError("read past a whole answer: the close comes a second late")
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def shutdown(self, how):
        pass

    def close(self):
        pass


def controls(peer):
    c = object.__new__(KubernetesVolumeControls)
    c.registration = SimpleNamespace(endpoint="https://api.test:6443", namespace="apps")
    c.token_provider = lambda: "token"
    c._pinned_tls = lambda deadline: peer
    return c


def test_masking_round_trips_at_every_length_boundary():
    for n in (0, 1, 125, 126, 65535, 65536, 70001):
        payload = bytes(range(256)) * (n // 256) + bytes(n % 256)
        frame = ws_frame(payload)
        assert client_frames(frame) == [(2, payload)]


def test_an_agent_answer_comes_back_through_the_port_forward():
    body = b"x" * 90000  # more than one frame each way
    peer = Peer(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    got = controls(peer).forward("PUT", "agent-1", 8080, "/write", {"claim": "0"},
                                 headers={"X-Sova-Agent-Token": "t"}, body=b"y" * 2_000_000)
    assert got == body
    request = peer.sent.split(b"\r\n\r\n", 1)
    assert request[0].startswith(b"GET /api/v1/namespaces/apps/pods/agent-1/portforward?"
                                 b"ports=8080 HTTP/1.1")
    sent = b"".join(p[1:] for _, p in client_frames(request[1]))  # channel 0 prefix
    assert sent.startswith(b"PUT /write?claim=0 HTTP/1.1\r\n") and sent.endswith(b"y" * 1000)
    assert b"X-Sova-Agent-Token: t\r\n" in sent and b"Content-Length: 2000000\r\n" in sent


@pytest.mark.parametrize("peer, refused", [
    (Peer(accept=b"bm90IHRoaXM="), ContractError),          # an answer to another key
    (Peer(protocol="channel.k8s.io"), ContractError),       # another protocol
    (Peer(status=403), KubernetesRequestRefused),
    (Peer(b"", error=b"pod not running"), ContractError),   # the error channel speaks
    (Peer(b"HTTP/1.1 200 OK\r\n\r\nno length"), ContractError),
    (Peer(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"), KubernetesRequestRefused),
])
def test_what_the_client_does_not_take(peer, refused):
    with pytest.raises(refused):
        controls(peer).forward("GET", "agent-1", 8080, "/manifest", {},
                               headers={"X-Sova-Agent-Token": "t"})


def test_only_exact_agent_requests_go_through():
    for method, path, headers in (("DELETE", "/x", {}), ("GET", "../x", {}),
                                  ("GET", "/x", {"Authorization": "t"})):
        with pytest.raises(ContractError):
            controls(Peer()).forward(method, "agent-1", 8080, path, {}, headers=headers)


def test_the_largest_agent_answer_fits_with_its_head():
    body = b"m" * 10
    peer = Peer(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n" + body)
    assert controls(peer).forward("GET", "agent-1", 8080, "/manifest", {},
                                  headers={"X-Sova-Agent-Token": "t"}, limit=64 << 20) == body


def test_a_whole_answer_ends_the_exchange_before_the_pod_side_closes():
    peer = Peer(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello", closes=False)
    assert controls(peer).forward("GET", "agent-1", 8080, "/healthz", {},
                                  headers={}) == b"hello"
