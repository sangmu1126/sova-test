"""Pinned HTTPS with fixed public read paths and bounded adapter-owned mutations."""

import base64
import hashlib
import json
import http.client
import os
import re
import socket
import ssl
import stat
import struct
import time
from threading import Timer
from urllib.parse import urlencode, urlsplit

from sova.adapters.kubernetes.api_json import decode_api_json
from sova.domain.contracts import ContractError

MAX_RESPONSE = 2 * 1024 * 1024
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_PROTOCOL = "v4.channel.k8s.io"
_WS_CHUNK = 1 << 20


def _masked(payload, key):
    """A client frame's payload masked with ``key`` (RFC 6455), at C speed."""
    if not payload:
        return b""
    pad = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(pad, "big")).to_bytes(
        len(payload), "big")


def ws_frame(payload, opcode=2):
    """One masked, final client frame."""
    key = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + key + _masked(payload, key)


class _Frames:
    """Reads an HTTP upgrade answer, then WebSocket frames, from a socket."""

    def __init__(self, sock, limit):
        self.sock, self.buffer, self.limit = sock, b"", limit

    def _fill(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise EOFError
            self.buffer += chunk

    def _take(self, n):
        self._fill(n)
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def head(self):
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > 65536:
                raise ContractError("Kubernetes upgrade answer too large")
            self._fill(len(self.buffer) + 1)
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        fields = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            fields[name.strip().lower()] = value.strip()
        return status, fields

    def frame(self):
        first, second = self._take(2)
        opcode, length = first & 0x0F, second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._take(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._take(8))[0]
        if length > self.limit + 64:
            raise ContractError("Kubernetes stream frame too large")
        key = self._take(4) if second & 0x80 else None
        data = self._take(length)
        return opcode, _masked(data, key) if key else data


def private_text(path, *, limit=65536):
    """Operator-only POSIX credential/config input; never echo content in errors."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ContractError("Private operator file permissions required")
            data = source.read(limit + 1)
            if len(data) > limit:
                raise ContractError("Operator file too large")
            return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError("Unreadable private operator file") from exc


_REASON = re.compile(r"[A-Za-z_]{1,40}")


def refusal_reason(body):
    """The reason token of a refused answer: an agent's ``error`` or the API
    server's ``Status.reason`` (``Forbidden``, ``NotFound``), never free text."""
    try:
        found = json.loads(body[:4096])
    except (ValueError, UnicodeDecodeError):
        return None
    reason = found.get("error", found.get("reason")) if type(found) is dict else None
    return reason if type(reason) is str and _REASON.fullmatch(reason) else None


class KubernetesTransportFailure(ContractError):
    """The connection failed before an answer: refused, reset, a TLS handshake
    or read that timed out, a peer that closed without answering."""


# A read (GET) is tried again after a failure of the connection itself, each
# time with its own deadline: the path to an API server over the internet
# drops now and then (measured against EKS: a TLS handshake timing out, a
# connection closed without an answer). A write is never repeated - whether
# it took effect is unknown, and its caller reads back instead.
_READ_ATTEMPTS = 3


class KubernetesRequestRefused(ContractError):
    def __init__(self, status, reason=None):
        self.status, self.reason = status, reason
        super().__init__(f"Registered Kubernetes request refused: HTTP {status}"
                         + (f" {reason}" if reason else ""))


class KubernetesHTTPS:
    def __init__(self, registration, *, ca_pem: bytes, token_provider):
        if hashlib.sha256(ca_pem).hexdigest() != registration.ca_digest:
            raise ContractError("Registered CA digest mismatch")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_verify_locations(cadata=ca_pem.decode("ascii"))
        except (ValueError, UnicodeError, ssl.SSLError) as exc:
            raise ContractError("Invalid registered CA") from exc
        self.registration = registration
        self.context = context
        self.token_provider = token_provider

    @property
    def registration_digest(self):
        return self.registration.fingerprint

    def get(self, path, query, *, timeout):
        reg = self.registration
        allowed = {"/api/v1/namespaces/" + reg.namespace}
        for base, plural in (
            ("/apis/apps/v1", "deployments"),
            ("/apis/apps/v1", "statefulsets"),
            ("/api/v1", "services"),
            ("/api/v1", "persistentvolumeclaims"),
        ):
            allowed.add(base + "/namespaces/" + reg.namespace + "/" + plural)
        if path not in allowed or set(query) - {"labelSelector", "limit", "continue"}:
            raise ContractError("Unregistered Kubernetes read path")
        if path == "/api/v1/namespaces/" + reg.namespace:
            if query:
                raise ContractError("Namespace query not allowed")
        elif (
            query.get("labelSelector")
            != "app.kubernetes.io/part-of=" + reg.application_id
            or query.get("limit") != "32"
        ):
            raise ContractError("Unregistered application query")
        return self._request("GET", path, query, timeout=timeout)

    def _pinned_tls(self, deadline):
        """A TLS socket to the API server: only its registered addresses, its
        registered CA and host name."""
        reg = self.registration
        endpoint = urlsplit(reg.endpoint)
        failure = None
        for registered in reg.approved_addresses:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                peer = socket.create_connection((registered, endpoint.port or 443),
                                                timeout=left)
            except OSError as error:
                failure = error
                continue
            peer.settimeout(max(deadline - time.monotonic(), 0.001))
            try:
                return self.context.wrap_socket(peer, server_hostname=endpoint.hostname)
            except (OSError, ssl.SSLError):
                peer.close()
                raise
        if failure is not None:
            raise failure  # every registered address refused
        raise ContractError("Kubernetes connect deadline exceeded")

    def _stream(self, path, query, payload, *, timeout, limit, complete=None):
        """One exchange with a Pod's port through the API server's port-forward
        (WebSocket, channel protocol v4): ``payload`` written to the port, what
        the Pod answers read until it closes the connection. On the way the
        bytes stay inside TLS - this client's verified one to the API server,
        the API server's own to the kubelet - and leave the node's network only
        so; nothing crosses the cluster network in the clear. ``complete(answer)``
        says when the answer is whole: the Pod closing its end reaches this client
        only about a second later, so an answer that knows its length ends here."""
        if (type(payload) is not bytes or len(payload) > (16 << 20) + 65536
                or type(limit) is not int or not 0 < limit <= (64 << 20) + 65536
                or type(timeout) not in (int, float) or not 0 < timeout <= 900
                or not path.endswith("/portforward")):  # the limit: an answer and its head
            raise ContractError("Unsupported registered Kubernetes stream")
        token = self.token_provider()
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ContractError("Invalid credential format")
        endpoint = urlsplit(self.registration.endpoint)
        deadline = time.monotonic() + timeout
        sock, timer = None, None
        try:
            sock = self._pinned_tls(deadline)

            def interrupt():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = Timer(max(deadline - time.monotonic(), 0.001), interrupt)
            timer.daemon = True
            timer.start()
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                f"GET {path}?{urlencode(query)} HTTP/1.1\r\nHost: {endpoint.netloc}\r\n"
                f"Authorization: Bearer {token}\r\nConnection: Upgrade\r\n"
                f"Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: {_WS_PROTOCOL}\r\n"
                "\r\n").encode())
            frames = _Frames(sock, limit)
            status, fields = frames.head()
            if status != 101:
                raise KubernetesRequestRefused(status)
            accept = base64.b64encode(hashlib.sha1(key.encode() + _WS_GUID).digest()).decode()
            if fields.get("sec-websocket-accept") != accept or \
                    fields.get("sec-websocket-protocol") != _WS_PROTOCOL:
                raise ContractError("Unexpected Kubernetes stream upgrade")
            for _ in range(2):  # the port's data and error channels, each naming the port
                opcode, data = frames.frame()
                if opcode != 2 or len(data) != 3 or data[0] not in (0, 1):
                    raise ContractError("Unexpected Kubernetes stream opening")
            for start in range(0, len(payload), _WS_CHUNK):
                sock.sendall(ws_frame(b"\x00" + payload[start:start + _WS_CHUNK]))
            out, errors = bytearray(), bytearray()
            while True:
                try:
                    opcode, data = frames.frame()
                except EOFError:
                    break
                if opcode == 8:  # closed: the Pod's side of the connection ended
                    break
                if opcode == 9:
                    sock.sendall(ws_frame(data, opcode=10))
                elif opcode in (1, 2) and data:
                    (out if data[0] == 0 else errors).extend(data[1:])
                if len(out) > limit:
                    raise ContractError("Kubernetes response limit exceeded")
                if complete is not None and not errors and complete(out):
                    break
            if time.monotonic() >= deadline:
                raise ContractError("Kubernetes read deadline exceeded")
            if errors:
                raise ContractError("Kubernetes port-forward failed: "
                                    + errors[:200].decode("utf-8", "replace"))
            return bytes(out)
        except ContractError:
            raise
        except (OSError, ssl.SSLError, ValueError, IndexError) as exc:
            raise ContractError("Kubernetes secure stream failed") from exc
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if sock is not None:
                sock.close()

    def _request(self, method, path, query, *, timeout, body=None, content_type=None,
                 accept=(200,), limit=MAX_RESPONSE, raw=False, headers=None, transfer=False):
        """Shared pinned transport. Public methods must fix their own API paths.

        This is not an arbitrary URL/manifest interface. The read-only public
        get() keeps its original allowlist; bounded mutation adapters add their
        own exact resource and JSON Patch validation before reaching here.

        By default a request is a small API call: GET or PATCH, a body of at
        most 64 KiB, five seconds, a JSON answer with status 200. An adapter
        that moves data (``transfer=True``) may send a body of up to 16 MiB and
        wait up to 15 minutes, and takes the answer as bytes (``raw``).
        """
        reg = self.registration
        body_limit = (16 << 20) + 4096 if transfer else 65536
        if (
            method not in {"GET", "PATCH", "POST", "PUT", "DELETE"}
            or (body is not None and (type(body) is not bytes or len(body) > body_limit))
            or (method == "GET" and body is not None)
            or (method in {"POST", "PUT"} and content_type is None)
            or not accept or not set(accept) <= {200, 201, 202}
            or type(limit) is not int or not 0 < limit <= (64 << 20)
            or (headers is not None and (type(headers) is not dict or any(
                type(k) is not str or not k.startswith("X-Sova-") or type(v) is not str
                or any(ord(c) < 33 or ord(c) > 126 for c in v) for k, v in headers.items())))
        ):
            raise ContractError("Unsupported registered Kubernetes request")
        if type(timeout) not in (int, float) or not 0 < timeout <= (900 if transfer else 5):
            raise ContractError("Invalid read timeout")
        token = self.token_provider()
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16384
            or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ContractError("Invalid credential format")
        attempts = _READ_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            try:
                return self._attempt(reg, token, method, path, query, timeout=timeout,
                                     body=body, content_type=content_type, accept=accept,
                                     limit=limit, raw=raw, headers=headers)
            except KubernetesTransportFailure:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise ContractError("Kubernetes request not attempted")  # unreachable

    def _attempt(self, reg, token, method, path, query, *, timeout, body, content_type,
                 accept, limit, raw, headers):
        """One exchange with the API server, within ``timeout``."""
        endpoint = urlsplit(reg.endpoint)
        connection = http.client.HTTPSConnection(
            endpoint.hostname,
            endpoint.port or 443,
            timeout=timeout,
            context=self.context,
        )
        deadline = time.monotonic() + timeout
        timer = None

        # TLS SNI/hostname verification still uses endpoint.hostname. Socket uses
        # only registered numeric IPs; DNS, environment proxies and redirects cannot change it.
        # A managed API endpoint has several addresses (EKS: one per zone); each
        # registered one is tried in turn within the same deadline, never any other.
        def connect_peer(address, timeout, source_address=None):
            failure = None
            for registered in reg.approved_addresses:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    peer = socket.create_connection(
                        (registered, endpoint.port or 443), timeout=min(timeout, left)
                    )
                except OSError as error:
                    failure = error
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    peer.close()
                    break
                peer.settimeout(left)
                return peer
            if failure is not None and deadline - time.monotonic() > 0:
                raise failure  # every registered address refused
            raise KubernetesTransportFailure("Kubernetes connect deadline exceeded")

        connection._create_connection = connect_peer
        try:
            connection.connect()
            left = deadline - time.monotonic()
            if left <= 0:
                raise ContractError("Kubernetes read deadline exceeded")
            peer = connection.sock

            def interrupt():
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            # Socket inactivity timeout alone does not bound a trickling response.
            timer = Timer(left, interrupt)
            timer.daemon = True
            timer.start()
            kind = content_type or ("application/json-patch+json" if body is not None else None)
            connection.request(
                method,
                path + ("?" + urlencode(query) if query else ""),
                body=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "*/*" if raw else "application/json",
                    "Accept-Encoding": "identity",
                    **({"Content-Type": kind} if kind is not None else {}),
                    **(headers or {}),
                },
            )
            response = connection.getresponse()
            if response.status not in accept:
                raise KubernetesRequestRefused(response.status,
                                               refusal_reason(response.read(4096)))
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ContractError("Unsupported Kubernetes response encoding")
            body = response.read(limit + 1)
            if time.monotonic() >= deadline:
                raise ContractError("Kubernetes read deadline exceeded")
            if len(body) > limit:
                raise ContractError("Kubernetes response limit exceeded")
            return body if raw else decode_api_json(body, limit=limit)
        except ContractError:
            raise
        except ssl.SSLCertVerificationError as exc:  # not the path: the peer itself
            raise ContractError("Kubernetes secure request failed") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise KubernetesTransportFailure("Kubernetes secure request failed") from exc
        except (UnicodeError, ValueError) as exc:
            raise ContractError("Kubernetes secure request failed") from exc
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            connection.close()
