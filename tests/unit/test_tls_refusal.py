"""TLS identity and plaintext refusal against local servers with a test CA.

Each server is a thread on loopback that answers as a real endpoint would:
TLS only (the handshake fails on plaintext and the connection closes), TLS
plus Go's "HTTP request to an HTTPS server" answer, TLS plus an S3 answer in
the clear, and PostgreSQL's SSLRequest with either access rule.
"""

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
import socket
import ssl
import struct
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from sova.adapters.tls_refusal import (
    TlsEndpoint,
    handshake,
    http_plaintext,
    postgres_plaintext,
    transport_refusals,
)
from sova.domain.contracts import ContractError


def _certificate(subject, issuer_name, public, signer, *, ca, names=()):
    now = datetime.now(UTC)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name)
               .public_key(public).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5))
               .not_valid_after(now + timedelta(hours=1))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(names)),
                                        critical=False)
    return builder.sign(signer, hashes.SHA256())


def material(names=("localhost",)):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls-refusal test CA")])
    ca = _certificate(ca_name, ca_name, ca_key.public_key(), ca_key, ca=True)
    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ip_address("127.0.0.1"))]
    cert = _certificate(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]),
                        ca_name, key.public_key(), ca_key, ca=False, names=sans)
    pem = serialization.Encoding.PEM
    return {"ca": ca.public_bytes(pem), "cert": cert.public_bytes(pem),
            "key": key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())}


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    found = material()
    root = tmp_path_factory.mktemp("tls")
    (root / "cert.pem").write_bytes(found["cert"])
    (root / "key.pem").write_bytes(found["key"])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    return found["ca"], context


class Server:
    """Accepts connections until stopped; ``mode`` decides each answer."""

    def __init__(self, context, mode):
        self.context, self.mode = context, mode
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(connection,), daemon=True).start()

    def _tls(self, connection):
        try:
            with self.context.wrap_socket(connection, server_side=True) as tls:
                tls.recv(16)
        except (ssl.SSLError, OSError):
            pass

    def _mysql(self, connection):
        flags = 0x200 | 0x8000 | 0x80000 | (0 if self.mode == "mysql-no-tls" else 0x800)
        greeting = (b"\x0a8.4.0\0" + struct.pack("<I", 7) + b"saltsalt\0"
                    + struct.pack("<HBHHB", flags & 0xFFFF, 255, 2, flags >> 16, 21)
                    + b"\0" * 10 + b"saltsaltsalt\0" + b"mysql_native_password\0")
        connection.sendall(len(greeting).to_bytes(3, "little") + b"\0" + greeting)
        head = connection.recv(4)
        body = connection.recv(int.from_bytes(head[:3], "little"))
        self.received.append(body)
        if struct.unpack("<I", body[:4])[0] & 0x800:
            return self._tls(connection)
        code = 3159 if self.mode == "mysql-required" else 1045
        error = b"\xff" + struct.pack("<H", code) + b"#HY000refused"
        connection.sendall(len(error).to_bytes(3, "little") + b"\x02" + error)

    def _one(self, connection):
        connection.settimeout(3)
        try:
            if self.mode.startswith("mysql"):
                return self._mysql(connection)
            if self.mode == "postgres-hostssl" or self.mode == "postgres-trust":
                head = connection.recv(8)
                self.received.append(head)
                if head == struct.pack("!ii", 8, 80877103):
                    connection.sendall(b"S")
                    return self._tls(connection)
                if self.mode == "postgres-trust":
                    connection.sendall(b"R" + struct.pack("!ii", 8, 3))  # cleartext password
                    return
                fields = (b"SFATAL\0C28000\0Mno pg_hba.conf entry for host \"127.0.0.1\", "
                          b"user \"app\", database \"orders\", no encryption\0\0")
                connection.sendall(b"E" + struct.pack("!i", len(fields) + 4) + fields)
                return
            first = connection.recv(1, socket.MSG_PEEK)
            if first == b"\x16" or self.mode == "tls-only":
                return self._tls(connection)
            self.received.append(connection.recv(4096))
            if self.mode == "go":
                connection.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n"
                                   b"Client sent an HTTP request to an HTTPS server.\n")
            elif self.mode == "s3-in-clear":
                connection.sendall(b"HTTP/1.1 403 Forbidden\r\nx-amz-request-id: 1\r\n"
                                   b"Content-Length: 25\r\n\r\n<Error>AccessDenied</Error>")
        finally:
            connection.close()

    def endpoint(self, ca, **kwargs):
        return TlsEndpoint("127.0.0.1", self.port, kwargs.pop("server_name", "localhost"), ca,
                           timeout_seconds=3, **kwargs)

    def stop(self):
        self.listener.close()


@pytest.fixture
def serve(tls):
    started = []

    def start(mode):
        started.append(Server(tls[1], mode))
        return started[-1]
    yield start
    for server in started:
        server.stop()


def test_a_tls_only_endpoint_refuses_a_wrong_ca_a_wrong_host_and_plaintext(tls, serve):
    server = serve("tls-only")
    passed, observation = transport_refusals(server.endpoint(tls[0]), plaintext=http_plaintext)
    assert passed, observation
    checks = observation["checks"]
    assert checks["positive_before"]["outcome"] == checks["positive_after"]["outcome"] == \
        "verified"
    assert checks["wrong_ca"]["verify_code"] in (19, 20)
    assert checks["wrong_host"]["verify_code"] == 62
    assert checks["plaintext"]["outcome"] == "refused"
    assert "BEGIN" not in str(observation)


def test_the_https_only_answer_is_a_refusal(tls, serve):
    passed, observation = transport_refusals(serve("go").endpoint(tls[0]),
                                             plaintext=http_plaintext)
    assert passed and observation["checks"]["plaintext"]["reason"] == "https_only_answer"


def test_an_endpoint_serving_in_the_clear_fails_decidedly(tls, serve):
    passed, observation = transport_refusals(serve("s3-in-clear").endpoint(tls[0]),
                                             plaintext=http_plaintext)
    assert not passed and observation["outcome"] == "accepted"
    assert observation["accepted"] == ["plaintext"]
    assert observation["checks"]["plaintext"]["http_status"] == 403


def test_an_unmeasured_plaintext_route_does_not_pass(tls, serve):
    passed, observation = transport_refusals(serve("tls-only").endpoint(tls[0]))
    assert not passed and observation["outcome"] == "undecided"


def test_an_endpoint_not_holding_the_registered_identity_is_not_verified(tls, serve):
    other_ca = material()["ca"]
    server = serve("tls-only")
    assert handshake(server.endpoint(other_ca))["outcome"] == "refused"
    passed, observation = transport_refusals(server.endpoint(other_ca), plaintext=http_plaintext)
    assert not passed and observation["outcome"] == "identity_not_verified"
    assert "wrong_ca" not in observation["checks"]  # nothing to bracket


def test_a_certificate_holding_the_name_tried_fails_the_wrong_host_check(tmp_path,
                                                                         monkeypatch):
    """OpenSSL never matches a wildcard over a single top label (``*.invalid``),
    so the check's name is held only by a certificate issued for it: that
    certificate verifies for the wrong host and the check fails decidedly."""
    monkeypatch.setattr("sova.adapters.tls_refusal.secrets.token_hex", lambda n: "abc")
    found = material(names=("localhost", "sova-wrong-abc.invalid"))
    (tmp_path / "c.pem").write_bytes(found["cert"])
    (tmp_path / "k.pem").write_bytes(found["key"])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    server = Server(context, "tls-only")
    try:
        passed, observation = transport_refusals(server.endpoint(found["ca"]),
                                                 plaintext=http_plaintext)
    finally:
        server.stop()
    assert not passed and observation["accepted"] == ["wrong_host"]


def test_postgres_tls_after_ssl_request_and_its_access_rules(tls, serve):
    strict = serve("postgres-hostssl")
    passed, observation = transport_refusals(
        strict.endpoint(tls[0], preamble="postgres"),
        plaintext=postgres_plaintext("app", "orders"))
    assert passed, observation
    assert observation["checks"]["plaintext"]["reason"] == "server_requires_tls"
    open_ = serve("postgres-trust")
    passed, observation = transport_refusals(
        open_.endpoint(tls[0], preamble="postgres"),
        plaintext=postgres_plaintext("app", "orders"))
    assert not passed and observation["accepted"] == ["plaintext"]
    assert observation["checks"]["plaintext"]["reason"] == "authentication_requested"


def test_nothing_listening_is_undecided_not_refused():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    endpoint = TlsEndpoint("127.0.0.1", port, "localhost", material()["ca"], timeout_seconds=1)
    passed, observation = transport_refusals(endpoint, plaintext=http_plaintext)
    assert not passed and observation["outcome"] == "undecided"


def test_an_endpoint_registration_is_complete():
    ca = material()["ca"]
    for bad in (dict(host="", port=1, server_name="x", ca_pem=ca),
                dict(host="h", port=0, server_name="x", ca_pem=ca),
                dict(host="h", port=1, server_name="x", ca_pem=b"not a cert"),
                dict(host="h", port=1, server_name="x", ca_pem=ca, preamble="smtp")):
        with pytest.raises(ContractError):
            TlsEndpoint(**bad)


def scripted(monkeypatch, answers):
    """Each handshake answers the next scripted outcome, in check order."""
    queue = list(answers)
    monkeypatch.setattr("sova.adapters.tls_refusal.handshake", lambda *a, **k: queue.pop(0))
    return TlsEndpoint("127.0.0.1", 1, "localhost", material()["ca"])


VERIFIED = {"outcome": "verified"}
WRONG_CA = {"outcome": "refused", "verify_code": 20}
WRONG_HOST = {"outcome": "refused", "verify_code": 62}
REFUSED_PLAIN = lambda endpoint: {"outcome": "refused"}  # noqa: E731


def test_a_refusal_of_the_wrong_kind_is_undecided(monkeypatch):
    """The wrong-CA attempt failed for an expired certificate (10), not for
    its chain: that says nothing about which CA the endpoint chains to."""
    endpoint = scripted(monkeypatch, [VERIFIED, {"outcome": "refused", "verify_code": 10},
                                      WRONG_HOST, VERIFIED])
    passed, observation = transport_refusals(endpoint, plaintext=REFUSED_PLAIN)
    assert not passed and observation["outcome"] == "undecided"


def test_refusals_after_the_endpoint_stopped_verifying_are_undecided(monkeypatch):
    endpoint = scripted(monkeypatch, [VERIFIED, WRONG_CA, WRONG_HOST,
                                      {"outcome": "undecided", "reason": "unreachable"}])
    passed, observation = transport_refusals(endpoint, plaintext=REFUSED_PLAIN)
    assert not passed and observation["outcome"] == "undecided"
    endpoint = scripted(monkeypatch, [VERIFIED, WRONG_CA, WRONG_HOST, VERIFIED])
    assert transport_refusals(endpoint, plaintext=REFUSED_PLAIN)[0]


def test_a_connection_closed_before_the_ssl_request_is_answered_is_undecided(serve, tls):
    """What a port-forward does while nothing listens behind it: the server
    declined nothing, so nothing is refused."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def close_at_once():
        connection, _ = listener.accept()
        connection.recv(8)
        connection.close()
    threading.Thread(target=close_at_once, daemon=True).start()
    try:
        found = handshake(TlsEndpoint("127.0.0.1", port, "localhost", tls[0],
                                      preamble="postgres", timeout_seconds=3))
    finally:
        listener.close()
    assert found["outcome"] == "undecided"


@pytest.mark.parametrize("module, cls, hello", [
    ("sova.adapters.redis_keyspace_probes", "RedisKeyspaceProbes", "REDIS_PING"),
    ("sova.adapters.rabbitmq_queue_probes", "RabbitMqQueueProbes", "AMQP_HEADER"),
    ("sova.adapters.kafka_topic_probes", "KafkaTopicProbes", "KAFKA_API_VERSIONS"),
    ("sova.adapters.mongodb_probes", "MongoDatabaseProbes", "MONGODB_HELLO"),
])
def test_each_units_probes_send_their_own_protocols_first_message(tls, serve, module, cls,
                                                                  hello):
    import importlib
    from types import SimpleNamespace

    import sova.adapters.tls_refusal as refusal

    probes_class = getattr(importlib.import_module(module), cls)
    server = serve("s3-in-clear")  # answers anything sent in the clear
    unit = SimpleNamespace(tls={"target": server.endpoint(tls[0])})
    passed, observation = probes_class.transport_refusals(unit, "target")
    assert not passed and observation["accepted"] == ["plaintext"]
    assert observation["side"] == "target"
    assert server.received == [getattr(refusal, hello)]
    assert not probes_class.transport_refusals(SimpleNamespace(tls={}), "target")[0]


def test_mysql_tls_after_the_greeting_and_its_secure_transport_rule(tls, serve):
    from sova.adapters.tls_refusal import mysql_plaintext

    strict = serve("mysql-required")
    passed, observation = transport_refusals(strict.endpoint(tls[0], preamble="mysql"),
                                             plaintext=mysql_plaintext("orders_app"))
    assert passed, observation
    assert observation["checks"]["plaintext"]["reason"] == "server_requires_tls"
    login = [b for b in strict.received if b"orders_app" in b][0]
    assert login.endswith(b"orders_app\0\0mysql_native_password\0")  # no password
    passed, observation = transport_refusals(serve("mysql-optional").endpoint(
        tls[0], preamble="mysql"), plaintext=mysql_plaintext("orders_app"))
    assert not passed and observation["accepted"] == ["plaintext"]
    assert observation["checks"]["plaintext"]["reason"] == "authenticated_in_the_clear"


def test_a_mysql_server_offering_no_tls_is_not_verified(tls, serve):
    from sova.adapters.tls_refusal import mysql_plaintext

    passed, observation = transport_refusals(serve("mysql-no-tls").endpoint(
        tls[0], preamble="mysql"), plaintext=mysql_plaintext("orders_app"))
    assert not passed and observation["outcome"] == "identity_not_verified"
    assert observation["checks"]["positive_before"]["reason"] == "tls_unavailable"
