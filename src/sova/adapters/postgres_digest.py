"""Content digest of one PostgreSQL table, computed where the rows are (ADR-0004).

The digest is the one the reference adapter and probes have always used: the
row count, and SHA-256 over every row's text form (UTF-8), sorted bytewise
and each prefixed with its length as 8 big-endian bytes. Equal content gives
an equal digest whatever the physical row order, the declared key's
uniqueness or the server's collation.

``table_digest`` computes it on the server: only the count and the hash cross
the network, instead of every row (measured: comparisons were most of the
write pause on a cross-cloud handoff). The same function on the client side
remains for tables above ``server_limit_bytes``, because the server builds
the framed rows of one table in memory before hashing.

A row's text form depends on session settings (time zone, date and interval
style, float digits, bytea output). Two servers with different defaults would
show equal data differently, so both ways pin them for the transaction.
"""

from psycopg import sql

from sova.adapters.row_digest import client_digest
from sova.domain.contracts import ContractError

SERVER_LIMIT_BYTES = 256 * 1024 * 1024
_SETTINGS = ("SET LOCAL TimeZone TO 'UTC'", "SET LOCAL DateStyle TO 'ISO, YMD'",
             "SET LOCAL IntervalStyle TO 'postgres'", "SET LOCAL extra_float_digits TO 3",
             "SET LOCAL bytea_output TO 'hex'")


def table_digest(conn, table, *, server_limit_bytes=SERVER_LIMIT_BYTES):
    """``{"rows", "sha256"}`` of ``table``; on the server when it is small enough."""
    if type(table) is not str or not table:
        raise ContractError("Table name required")
    name = sql.Identifier(table)
    with conn.transaction():
        for setting in _SETTINGS:
            conn.execute(setting)
        size = conn.execute("SELECT pg_total_relation_size(%s::regclass)",
                            (name.as_string(conn),)).fetchone()[0]
        if size <= server_limit_bytes:
            count, digest = conn.execute(sql.SQL(
                "SELECT count(*), encode(sha256(coalesce(string_agg("
                "int8send(octet_length(r.b)) || r.b, ''::bytea ORDER BY r.b), ''::bytea)), "
                "'hex') FROM (SELECT convert_to(t::text, 'UTF8') AS b FROM {} t) r").format(
                    name)).fetchone()
            return {"rows": count, "sha256": digest}
        return client_digest(text for (text,) in conn.execute(
            sql.SQL("SELECT t::text FROM {} t").format(name)))
