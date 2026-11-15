"""Ask one name server for the A records of one name, over DNS itself.

What a zone's own authoritative servers answer is the observation a route
store needs where the provider has no answer-testing API (Cloud DNS). The
query asks without recursion, so only what that server is authoritative for
comes back. Only the standard library is used: one UDP datagram out, one in,
with the reply's ID, flags and question checked before any record is taken.
A truncated reply is not trusted (no records are returned for it).
"""

import ipaddress
import secrets
import socket
import struct

from sova.domain.contracts import ContractError

RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
_A, _IN = 1, 1


def _encode(name):
    labels = name.rstrip(".").split(".")
    if not labels or any(not 0 < len(label) < 64 for label in labels):
        raise ContractError("DNS name required")
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\0"


def _skip_name(message, offset):
    while True:
        if offset >= len(message):
            raise ValueError("name runs past the message")
        length = message[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


def _read_name(message, offset, depth=0):
    labels = []
    while True:
        if depth > 16 or offset >= len(message):
            raise ValueError("bad name")
        length = message[offset]
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack("!H", message[offset:offset + 2])[0] & 0x3FFF
            labels.append(_read_name(message, pointer, depth + 1))
            return ".".join(label for label in labels if label)
        if length == 0:
            return ".".join(labels)
        labels.append(message[offset + 1:offset + 1 + length].decode("ascii").lower())
        offset += 1 + length


def parse_reply(message, query_id, name):
    """``(rcode, authoritative, addresses)`` from a reply to our query."""
    if len(message) < 12:
        raise ValueError("short reply")
    ident, flags, qd, an, _, _ = struct.unpack("!6H", message[:12])
    if ident != query_id or not flags & 0x8000:
        raise ValueError("not the reply to this query")
    if flags & 0x0200:  # a truncated answer is not taken as the answer
        return "TRUNCATED", bool(flags & 0x0400), []
    offset = 12
    for _ in range(qd):
        if _read_name(message, offset) != name.rstrip(".").lower():
            raise ValueError("reply for another question")
        offset = _skip_name(message, offset) + 4
    addresses = []
    for _ in range(an):
        owner = _read_name(message, offset)
        offset = _skip_name(message, offset)
        rtype, rclass, _, length = struct.unpack("!HHIH", message[offset:offset + 10])
        offset += 10
        data = message[offset:offset + length]
        offset += length
        if rtype == _A and rclass == _IN and length == 4 and owner == name.rstrip(".").lower():
            addresses.append(str(ipaddress.IPv4Address(data)))
    return RCODES.get(flags & 0x000F, str(flags & 0x000F)), bool(flags & 0x0400), sorted(addresses)


def query_a(server, name, *, timeout=3.0, port=53):
    """Ask ``server`` (host name or address) for ``name`` A, without recursion."""
    query_id = secrets.randbits(16)
    question = struct.pack("!6H", query_id, 0, 1, 0, 0, 0) + _encode(name) + struct.pack(
        "!HH", _A, _IN)
    family, _, _, _, address = socket.getaddrinfo(server, port, type=socket.SOCK_DGRAM)[0]
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(question, address)
        while True:  # ignore anything that is not the reply to this query
            reply, sender = sock.recvfrom(4096)
            if sender[0] != address[0]:
                continue
            try:
                return parse_reply(reply, query_id, name)
            except ValueError:
                continue
