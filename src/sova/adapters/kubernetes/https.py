"""Pinned HTTPS with fixed public read paths and bounded adapter-owned mutations."""

import hashlib
from datetime import UTC, datetime
import http.client
import os
import socket
import ssl
import stat
import time
from threading import Timer
from urllib.parse import urlencode, urlsplit

from sova.adapters.kubernetes.api_json import decode_api_json
from sova.domain.contracts import ContractError

MAX_RESPONSE = 2 * 1024 * 1024


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


class KubernetesRequestRefused(ContractError):
    def __init__(self, status):
        self.status = status
        super().__init__("Registered Kubernetes request refused")


class KubernetesHTTPS:
    def __init__(self, registration, *, ca_pem: bytes, token_provider, clock=lambda: datetime.now(UTC)):
        if not callable(clock):
            raise ContractError("Trusted Kubernetes request clock required")
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
        self.clock = clock

    @property
    def registration_digest(self):
        return self.registration.fingerprint

    def get(self, path, query, *, timeout):
        reg = self.registration
        reg.require_current(self.clock())
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
        reg.require_current(self.clock())
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
        now = self.clock()
        reg.require_current(now)
        timeout = min(timeout, (reg.valid_until - now).total_seconds())
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16384
            or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ContractError("Invalid credential format")
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
            raise ContractError("Kubernetes connect deadline exceeded")

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
                raise KubernetesRequestRefused(response.status)
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ContractError("Unsupported Kubernetes response encoding")
            body = response.read(limit + 1)
            if time.monotonic() >= deadline:
                raise ContractError("Kubernetes read deadline exceeded")
            if len(body) > limit:
                raise ContractError("Kubernetes response limit exceeded")
            reg.require_current(self.clock())
            return body if raw else decode_api_json(body, limit=limit)
        except ContractError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError, ValueError) as exc:
            raise ContractError("Kubernetes secure request failed") from exc
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            connection.close()
