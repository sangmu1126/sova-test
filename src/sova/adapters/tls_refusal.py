"""Whether an endpoint keeps its TLS identity and refuses plaintext (SEC-04).

Each unit's ``transport_encrypted`` probe checks the probe's own channel: that
it is TLS and that this client verified the server. That says nothing about
how the endpoint answers a client configured worse. These checks ask the
endpoint itself, bracketed by a positive control, the way the team's control
runner judged its ENC-01.1 to ENC-01.3 tests:

1. positive - a handshake trusting only the registered CA, naming the
   registered server name, must verify;
2. wrong CA - a handshake trusting only a CA made for this check (generated
   in memory, trusted nowhere else, discarded after) must be refused by
   certificate verification; otherwise the endpoint's certificate chains to a
   CA the registration does not name;
3. wrong host - the registered CA and a name the endpoint cannot hold
   (``sova-wrong-<random>.invalid``) must be refused by host-name
   verification; a certificate valid for any name is no identity;
4. plaintext - the protocol's own first message, sent without TLS, must not
   be served (``plaintext`` names how: ``http_plaintext``,
   ``postgres_plaintext``). A closed connection, a TLS alert, or the server's
   explicit TLS-only refusal is a refusal; an answer of the service itself is
   served - even an error, since the service read the request in the clear;
5. positive again.

A refusal counts only between two positive controls that verified, and only
of the expected kind (a wrong CA refused for its chain, a wrong host for its
name); anything else is undecided, never a pass. No credential is sent on any
of these connections: the plaintext attempts stop where the server would ask
for one. Observations carry digests of the binding and the certificate,
never key material.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
import socket
import ssl
import struct

from sova.domain.contracts import ContractError

PREAMBLES = (None, "postgres")
_POSTGRES_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
# X509 verification results (OpenSSL x509_vfy.h) that mean "not issued by a
# CA this client trusts", and "not issued for this name".
_UNTRUSTED_CHAIN = frozenset({2, 18, 19, 20, 21})
_WRONG_NAME = frozenset({62, 64})
_HTTPS_ONLY = re.compile(rb"https|\btls\b|\bssl\b", re.IGNORECASE)
_READ_LIMIT = 65536


@dataclass(frozen=True)
class TlsEndpoint:
    """A registered TLS endpoint: where it is, the name its certificate must
    hold, and the CA it must chain to. ``preamble`` is the protocol's step
    before the handshake: None for TLS from the first byte, ``postgres`` for
    PostgreSQL's SSLRequest."""

    host: str
    port: int
    server_name: str
    ca_pem: bytes = field(repr=False)
    preamble: str | None = None
    timeout_seconds: float = 5.0

    def __post_init__(self):
        if type(self.host) is not str or not self.host or type(self.server_name) is not str \
                or not self.server_name:
            raise ContractError("Endpoint host and server name required")
        if type(self.port) is not int or not 0 < self.port < 65536:
            raise ContractError("Endpoint port 1 to 65535 required")
        if type(self.ca_pem) is not bytes or b"BEGIN CERTIFICATE" not in self.ca_pem:
            raise ContractError("The registered CA certificate (PEM) required")
        if self.preamble not in PREAMBLES:
            raise ContractError(f"Preamble one of {PREAMBLES} required")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 30:
            raise ContractError("Timeout 0 to 30 seconds required")

    def binding(self):
        """A digest naming what was checked: address, name, CA, preamble."""
        text = "|".join((self.host, str(self.port), self.server_name,
                         hashlib.sha256(self.ca_pem).hexdigest(), str(self.preamble)))
        return hashlib.sha256(text.encode()).hexdigest()


def _throwaway_ca():
    """A self-signed CA certificate made for one check and trusted nowhere."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         f"sova wrong-CA check {secrets.token_hex(4)}")])
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                                  critical=True)
                   .sign(key, hashes.SHA256()))
    return certificate.public_bytes(serialization.Encoding.PEM)


def _context(ca_pem):
    context = ssl.create_default_context(cadata=ca_pem.decode("ascii"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _connect(endpoint):
    connection = socket.create_connection((endpoint.host, endpoint.port),
                                          timeout=endpoint.timeout_seconds)
    if endpoint.preamble == "postgres":
        connection.sendall(_POSTGRES_SSL_REQUEST)
        answer = connection.recv(1)
        if answer != b"S":
            connection.close()
            raise _NoTls("server declined TLS" if answer == b"N" else "no SSLRequest answer")
    return connection


class _NoTls(Exception):
    pass


def handshake(endpoint, *, ca_pem=None, server_name=None):
    """One TLS handshake to ``endpoint``, trusting only ``ca_pem`` (default the
    registered CA) and naming ``server_name`` (default the registered name).
    Returns ``{"outcome": "verified" | "refused" | "undecided", ...}``."""
    try:
        connection = _connect(endpoint)
    except _NoTls as error:
        return {"outcome": "refused", "reason": "tls_unavailable", "detail": str(error)}
    except (OSError, TimeoutError) as error:
        return {"outcome": "undecided", "reason": f"unreachable:{type(error).__name__}"}
    try:
        with _context(ca_pem or endpoint.ca_pem).wrap_socket(
                connection, server_hostname=server_name or endpoint.server_name) as tls:
            der = tls.getpeercert(binary_form=True) or b""
            return {"outcome": "verified", "tls_version": tls.version(),
                    "certificate_sha256": hashlib.sha256(der).hexdigest()}
    except ssl.SSLCertVerificationError as error:
        return {"outcome": "refused", "reason": "certificate_verification",
                "verify_code": error.verify_code, "detail": str(error.verify_message)[:120]}
    except ssl.SSLError as error:
        return {"outcome": "undecided", "reason": f"tls_error:{error.reason}"}
    except (OSError, TimeoutError) as error:
        return {"outcome": "undecided", "reason": f"handshake_failed:{type(error).__name__}"}
    finally:
        connection.close()


def _read_some(connection):
    data = b""
    try:
        while len(data) < _READ_LIMIT:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, BrokenPipeError):
        return data, "reset"
    except (TimeoutError, socket.timeout):
        return data, "timeout"
    return data, "closed"


def http_plaintext(endpoint):
    """An HTTP request in the clear on the endpoint's own port."""
    try:
        connection = socket.create_connection((endpoint.host, endpoint.port),
                                              timeout=endpoint.timeout_seconds)
    except (OSError, TimeoutError) as error:
        return {"outcome": "undecided", "reason": f"unreachable:{type(error).__name__}"}
    try:
        try:
            connection.sendall(f"GET / HTTP/1.1\r\nHost: {endpoint.server_name}\r\n"
                               "User-Agent: sova-tls-check\r\nConnection: close\r\n\r\n"
                               .encode())
        except OSError:
            return {"outcome": "refused", "reason": "closed_before_request"}
        data, end = _read_some(connection)
    finally:
        connection.close()
    if not data:
        if end == "timeout":
            return {"outcome": "undecided", "reason": "no_answer"}
        return {"outcome": "refused", "reason": f"connection_{end}"}
    if data[:1] == b"\x15":  # a TLS alert record
        return {"outcome": "refused", "reason": "tls_alert"}
    if not data.startswith(b"HTTP/"):
        return {"outcome": "undecided", "reason": "not_http"}
    status_line = data.split(b"\r\n", 1)[0]
    parts = status_line.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    body = data.split(b"\r\n\r\n", 1)[-1]
    if status == 400 and _HTTPS_ONLY.search(body):
        return {"outcome": "refused", "reason": "https_only_answer", "http_status": status}
    return {"outcome": "served", "http_status": status}


def postgres_plaintext(user, database):
    """A PostgreSQL startup message without SSLRequest, as ``user`` to
    ``database``. The server's access rules are applied before any password
    is asked for: a request for one means the server would authenticate in
    the clear, and no password is ever sent."""
    def attempt(endpoint):
        try:
            connection = socket.create_connection((endpoint.host, endpoint.port),
                                                  timeout=endpoint.timeout_seconds)
        except (OSError, TimeoutError) as error:
            return {"outcome": "undecided", "reason": f"unreachable:{type(error).__name__}"}
        try:
            fields = b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in (
                ("user", user), ("database", database),
                ("application_name", "sova-tls-check"))) + b"\0"
            payload = struct.pack("!i", 196608) + fields
            connection.sendall(struct.pack("!i", len(payload) + 4) + payload)
            head = connection.recv(5)
            if len(head) < 5:
                return {"outcome": "refused", "reason": "connection_closed"}
            kind, length = head[:1], struct.unpack("!i", head[1:])[0]
            body = b""
            while len(body) < min(length - 4, _READ_LIMIT):
                chunk = connection.recv(min(length - 4, _READ_LIMIT) - len(body))
                if not chunk:
                    break
                body += chunk
        except (OSError, TimeoutError) as error:
            return {"outcome": "undecided", "reason": f"exchange_failed:{type(error).__name__}"}
        finally:
            connection.close()
        if kind == b"R":
            return {"outcome": "served", "reason": "authentication_requested"}
        if kind == b"E":
            fields = dict((part[:1], part[1:]) for part in body.split(b"\0") if part)
            code = fields.get(b"C", b"").decode(errors="replace")
            message = fields.get(b"M", b"")
            if code == "28000" and b"encryption" in message:
                return {"outcome": "refused", "reason": "server_requires_tls",
                        "sqlstate": code}
            return {"outcome": "undecided", "reason": f"server_error:{code}"}
        return {"outcome": "undecided", "reason": f"unexpected_message:{kind!r}"}
    return attempt


def transport_refusals(endpoint, *, plaintext=None):
    """(passed, observation): the endpoint refuses a wrong CA, a wrong host
    name and - when ``plaintext`` is given - plaintext, between two positive
    controls. Without ``plaintext`` the observation says it was not checked,
    and the check does not pass: a plaintext route left unmeasured is not a
    refusal."""
    if not isinstance(endpoint, TlsEndpoint):
        raise ContractError("A registered TLS endpoint required")
    checks = {"positive_before": handshake(endpoint)}
    decided = checks["positive_before"]["outcome"] == "verified"
    if decided:
        checks["wrong_ca"] = handshake(endpoint, ca_pem=_throwaway_ca())
        checks["wrong_host"] = handshake(
            endpoint, server_name=f"sova-wrong-{secrets.token_hex(6)}.invalid")
        checks["plaintext"] = plaintext(endpoint) if plaintext else {
            "outcome": "undecided", "reason": "not_measured"}
        checks["positive_after"] = handshake(endpoint)
        decided = checks["positive_after"]["outcome"] == "verified"
    refused = {
        "wrong_ca": checks.get("wrong_ca", {}).get("outcome") == "refused"
        and checks["wrong_ca"].get("verify_code") in _UNTRUSTED_CHAIN,
        "wrong_host": checks.get("wrong_host", {}).get("outcome") == "refused"
        and checks["wrong_host"].get("verify_code") in _WRONG_NAME,
        "plaintext": checks.get("plaintext", {}).get("outcome") == "refused",
    }
    accepted = [name for name in ("wrong_ca", "wrong_host")
                if checks.get(name, {}).get("outcome") == "verified"]
    if checks.get("plaintext", {}).get("outcome") == "served":
        accepted.append("plaintext")
    if accepted:
        outcome = "accepted"
    elif checks["positive_before"]["outcome"] == "refused":
        outcome = "identity_not_verified"  # no TLS, or not the registered identity
    elif decided and all(refused.values()):
        outcome = "refused"
    else:
        outcome = "undecided"
    return outcome == "refused", {
        "probe": "transport_refusals", "endpoint_sha256": endpoint.binding(),
        "outcome": outcome, "accepted": accepted, "checks": checks}
