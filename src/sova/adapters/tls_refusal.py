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
   ``postgres_plaintext``, ``mysql_plaintext``, ``hello_plaintext`` with ``REDIS_PING``,
   ``AMQP_HEADER``, ``KAFKA_API_VERSIONS`` or ``MONGODB_HELLO``) - on the
   endpoint's port and on every port the service may also listen on in the
   clear (``plaintext_ports``: Redis keeps 6379 beside its TLS port). A
   closed connection or port, a TLS alert, or the server's explicit TLS-only
   refusal is a refusal; an answer of the service itself is served - even an
   error, since the service read the request in the clear;
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

PREAMBLES = (None, "postgres", "mysql")
_POSTGRES_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
# MySQL client capability flags (the protocol's own constants)
_MYSQL_PROTOCOL_41, _MYSQL_SSL, _MYSQL_SECURE, _MYSQL_PLUGIN_AUTH = 0x200, 0x800, 0x8000, 0x80000
_MYSQL_SECURE_TRANSPORT_REQUIRED = 3159
# X509 verification results (OpenSSL x509_vfy.h) that mean "not issued by a
# CA this client trusts", and "not issued for this name".
_UNTRUSTED_CHAIN = frozenset({2, 18, 19, 20, 21})
_WRONG_NAME = frozenset({62, 64})
_HTTPS_ONLY = re.compile(rb"https|\btls\b|\bssl\b", re.IGNORECASE)
_READ_LIMIT = 65536


@dataclass(frozen=True)
class TlsEndpoint:
    """A registered TLS endpoint: where it is, the name its certificate must
    hold, and the CA it must chain to (``ca_pem``, or ``ca_file`` read when a
    check runs, as the unit's own clients read it). ``preamble`` is the
    protocol's step before the handshake: None for TLS from the first byte,
    ``postgres`` for PostgreSQL's SSLRequest, ``mysql`` for the server's
    greeting and MySQL's SSLRequest."""

    host: str
    port: int
    server_name: str
    ca_pem: bytes | None = field(default=None, repr=False)
    preamble: str | None = None
    timeout_seconds: float = 5.0
    plaintext_ports: tuple[int, ...] = ()
    ca_file: str | None = None

    @property
    def ca(self):
        if self.ca_pem is not None:
            return self.ca_pem
        from pathlib import Path

        found = Path(self.ca_file).read_bytes()
        if b"BEGIN CERTIFICATE" not in found:
            raise ContractError("The registered CA file holds no certificate")
        return found

    def __post_init__(self):
        if type(self.host) is not str or not self.host or type(self.server_name) is not str \
                or not self.server_name:
            raise ContractError("Endpoint host and server name required")
        if type(self.port) is not int or not 0 < self.port < 65536:
            raise ContractError("Endpoint port 1 to 65535 required")
        if (self.ca_pem is None) == (self.ca_file is None):
            raise ContractError("The registered CA: its certificate (PEM) or its file")
        if self.ca_pem is not None and (type(self.ca_pem) is not bytes
                                        or b"BEGIN CERTIFICATE" not in self.ca_pem):
            raise ContractError("The registered CA certificate (PEM) required")
        if self.ca_file is not None and (type(self.ca_file) is not str or not self.ca_file):
            raise ContractError("The registered CA file's path required")
        if self.preamble not in PREAMBLES:
            raise ContractError(f"Preamble one of {PREAMBLES} required")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 30:
            raise ContractError("Timeout 0 to 30 seconds required")
        if type(self.plaintext_ports) is not tuple or not all(
                type(p) is int and 0 < p < 65536 for p in self.plaintext_ports):
            raise ContractError("plaintext_ports: a tuple of ports")

    def binding(self):
        """A digest naming what was checked: address, name, CA, preamble."""
        text = "|".join((self.host, str(self.port), self.server_name,
                         hashlib.sha256(self.ca).hexdigest(), str(self.preamble),
                         ",".join(map(str, self.plaintext_ports))))
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


def _mysql_packet(connection):
    """(sequence, payload) of one MySQL packet (payload bounded)."""
    head = b""
    while len(head) < 4:
        chunk = connection.recv(4 - len(head))
        if not chunk:
            raise ConnectionError("closed")
        head += chunk
    length = min(int.from_bytes(head[:3], "little"), _READ_LIMIT)
    body = b""
    while len(body) < length:
        chunk = connection.recv(length - len(body))
        if not chunk:
            raise ConnectionError("closed")
        body += chunk
    return head[3], body


def _mysql_capabilities(greeting):
    """The server's capability flags from its HandshakeV10, or None."""
    if not greeting or greeting[0] != 0x0A or b"\0" not in greeting[1:]:
        return None
    at = greeting.index(b"\0", 1) + 1 + 4 + 8 + 1  # version, thread id, salt, filler
    lower = int.from_bytes(greeting[at:at + 2], "little")
    upper = int.from_bytes(greeting[at + 5:at + 7], "little") if len(greeting) >= at + 7 else 0
    return lower | (upper << 16)


def _mysql_client(flags, rest=b""):
    payload = struct.pack("<IIB", flags, 1 << 24, 255) + b"\0" * 23 + rest
    return len(payload).to_bytes(3, "little") + b"\x01" + payload


def _connect(endpoint):
    connection = socket.create_connection((endpoint.host, endpoint.port),
                                          timeout=endpoint.timeout_seconds)
    if endpoint.preamble == "mysql":
        _, greeting = _mysql_packet(connection)
        flags = _mysql_capabilities(greeting)
        if flags is None:
            connection.close()
            raise ConnectionError("no MySQL greeting")
        if not flags & _MYSQL_SSL:
            connection.close()
            raise _NoTls("server offers no TLS")
        connection.sendall(_mysql_client(
            _MYSQL_PROTOCOL_41 | _MYSQL_SSL | _MYSQL_SECURE | _MYSQL_PLUGIN_AUTH))
    if endpoint.preamble == "postgres":
        connection.sendall(_POSTGRES_SSL_REQUEST)
        answer = connection.recv(1)
        if answer == b"N":
            connection.close()
            raise _NoTls("server declined TLS")
        if answer != b"S":  # closed or garbled: nothing was declined
            connection.close()
            raise ConnectionError("no answer to the SSLRequest")
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
        with _context(ca_pem or endpoint.ca).wrap_socket(
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


def _bson(fields):
    """A BSON document of int32 and string fields, in order."""
    body = b""
    for name, value in fields:
        key = name.encode() + b"\0"
        if type(value) is int:
            body += b"\x10" + key + struct.pack("<i", value)
        else:
            text = value.encode() + b"\0"
            body += b"\x02" + key + struct.pack("<i", len(text)) + text
    return struct.pack("<i", len(body) + 5) + body + b"\0"


REDIS_PING = b"PING\r\n"
AMQP_HEADER = b"AMQP\x00\x00\x09\x01"
KAFKA_API_VERSIONS = (struct.pack("!ihhih", 10 + len(b"sova-tls-check"), 18, 0, 1,
                                  len(b"sova-tls-check")) + b"sova-tls-check")
_HELLO = _bson([("hello", 1), ("$db", "admin")])
MONGODB_HELLO = struct.pack("<iiii", 16 + 5 + len(_HELLO), 1, 0, 2013) + \
    struct.pack("<I", 0) + b"\0" + _HELLO


def hello_plaintext(hello):
    """The protocol's first message, in the clear, on the endpoint's port and
    on each of its ``plaintext_ports``. A service answering in its protocol
    on any of them serves plaintext."""
    def one(endpoint, port):
        try:
            connection = socket.create_connection((endpoint.host, port),
                                                  timeout=endpoint.timeout_seconds)
        except ConnectionRefusedError:
            return {"port": port, "outcome": "refused", "reason": "port_closed"}
        except (OSError, TimeoutError) as error:
            return {"port": port, "outcome": "undecided",
                    "reason": f"unreachable:{type(error).__name__}"}
        try:
            try:
                connection.sendall(hello)
            except OSError:
                return {"port": port, "outcome": "refused", "reason": "closed_before_request"}
            connection.settimeout(min(endpoint.timeout_seconds, 3))
            try:
                data = connection.recv(4096)
            except (ConnectionResetError, BrokenPipeError):
                return {"port": port, "outcome": "refused", "reason": "connection_reset"}
            except (TimeoutError, socket.timeout):
                return {"port": port, "outcome": "undecided", "reason": "no_answer"}
        finally:
            connection.close()
        if not data:
            return {"port": port, "outcome": "refused", "reason": "connection_closed"}
        if data[:1] == b"\x15":
            return {"port": port, "outcome": "refused", "reason": "tls_alert"}
        return {"port": port, "outcome": "served", "answered_bytes": len(data)}

    def attempt(endpoint):
        ports = [one(endpoint, port) for port in (endpoint.port, *endpoint.plaintext_ports)]
        outcomes = [p["outcome"] for p in ports]
        outcome = ("served" if "served" in outcomes
                   else "refused" if set(outcomes) == {"refused"} else "undecided")
        return {"outcome": outcome, "ports": ports}
    return attempt


def settings_endpoints(found, plaintext_ports=None):
    """``{side: TlsEndpoint}`` for each side a unit's settings reach over
    verified TLS: ``found`` maps side to ``(host, port, ca_file)`` or None
    (that side is not TLS); ``plaintext_ports`` the ports per side the service
    may also serve in the clear."""
    ports = plaintext_ports or {}
    if type(ports) is not dict or set(ports) - {"source", "target"} or not all(
            type(v) is list for v in ports.values()):
        raise ContractError("plaintext_ports: a list of ports per side")
    return {side: TlsEndpoint(host, int(port), host, ca_file=ca,
                              plaintext_ports=tuple(ports.get(side, ())))
            for side, value in found.items() if value for host, port, ca in (value,)}


def check_tls(tls, preamble=None):
    """A unit's registered endpoints: ``{side: TlsEndpoint}``, or none."""
    tls = dict(tls or {})
    if set(tls) - {"source", "target"} or not all(
            isinstance(e, TlsEndpoint) and e.preamble == preamble for e in tls.values()):
        raise ContractError("tls: a TlsEndpoint per side")
    return tls


def side_refusals(tls, side, plaintext):
    """``transport_refusals`` of the side's registered endpoint, for a unit's
    probes: not passed when none is registered."""
    endpoint = (tls or {}).get(side)
    if endpoint is None:
        return False, {"probe": "transport_refusals", "side": side,
                       "reason": "no TLS endpoint registered"}
    passed, observation = transport_refusals(endpoint, plaintext=plaintext)
    return passed, {**observation, "side": side}


def mysql_plaintext(user):
    """A MySQL login as ``user`` without TLS and with an empty password. A
    server requiring secure transport answers 3159; one that goes on to
    authenticate in the clear (access denied, a method switch, or OK) serves
    plaintext. No password is sent."""
    def attempt(endpoint):
        try:
            connection = socket.create_connection((endpoint.host, endpoint.port),
                                                  timeout=endpoint.timeout_seconds)
        except (OSError, TimeoutError) as error:
            return {"outcome": "undecided", "reason": f"unreachable:{type(error).__name__}"}
        try:
            _, greeting = _mysql_packet(connection)
            if _mysql_capabilities(greeting) is None:
                return {"outcome": "undecided", "reason": "no_greeting"}
            connection.sendall(_mysql_client(
                _MYSQL_PROTOCOL_41 | _MYSQL_SECURE | _MYSQL_PLUGIN_AUTH,
                user.encode() + b"\0" + b"\0" + b"mysql_native_password\0"))
            _, answer = _mysql_packet(connection)
        except ConnectionError:
            return {"outcome": "refused", "reason": "connection_closed"}
        except (OSError, TimeoutError) as error:
            return {"outcome": "undecided", "reason": f"exchange_failed:{type(error).__name__}"}
        finally:
            connection.close()
        if answer[:1] == b"\xff":
            code = int.from_bytes(answer[1:3], "little")
            if code == _MYSQL_SECURE_TRANSPORT_REQUIRED:
                return {"outcome": "refused", "reason": "server_requires_tls", "code": code}
            if code == 1045:
                return {"outcome": "served", "reason": "authenticated_in_the_clear", "code": code}
            return {"outcome": "undecided", "reason": f"server_error:{code}"}
        if answer[:1] in (b"\x00", b"\x01", b"\xfe"):
            return {"outcome": "served", "reason": "authentication_continued"}
        return {"outcome": "undecided", "reason": "unexpected_answer"}
    return attempt


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
