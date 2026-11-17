"""Volume agent: a short-lived Pod's view of one side's PersistentVolumeClaims.

The volume handoff adapter starts this program, unchanged, in a Pod that
mounts the claims of one side (read-only on a source, read-write on a target
being filled) and talks to it through the API server's Pod proxy. It uses the
standard library only and runs as ``python3 -I -c <this source>``, so any
pinned Python image carries it and no image has to be built for it.

What it guarantees, whoever calls it:

* Every path is resolved one component at a time from the claim's mount with
  ``O_NOFOLLOW``: a symbolic link in the volume is recorded as a link and is
  never followed, so nothing outside the volume is read or written.
* A reader (``SOVA_AGENT_MODE=reader``) refuses every change, on top of its
  read-only mount.
* Every request but ``/healthz`` carries a token whose SHA-256 the Pod was
  started with; the adapter derives the token, the Pod spec holds only its
  digest.
* A file reaches its final name only after its whole content was written to a
  temporary name in the same directory, checked against the expected SHA-256
  and flushed; the rename is then flushed with its directory.

The manifest lists every entry under a claim's root: ``[path, kind, mode, uid,
gid, size, digest]`` with kind ``d`` (directory), ``f`` (file, digest = SHA-256
of the content) or ``l`` (symbolic link, digest = its target), in path order.
Anything else (a socket, a FIFO, a device) or anything unreadable is listed as
unsupported; the adapter then refuses to call the copy done.
"""

import hashlib
import hmac
import http.server
import json
import os
import stat
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = os.environ.get("SOVA_AGENT_ROOT", "/v")
MODE = os.environ.get("SOVA_AGENT_MODE", "reader")
TOKEN = os.environ.get("SOVA_AGENT_TOKEN_SHA256", "")
CLAIMS = int(os.environ.get("SOVA_AGENT_CLAIMS", "1"))
PORT = int(os.environ.get("SOVA_AGENT_PORT", "8080"))
CHUNK = 16 << 20  # the most one request may carry
MAX_ENTRIES = 500_000
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def components(path):
    if type(path) is not str or not path or len(path) > 4096 or "\0" in path:
        raise Refused(400, "bad_path")
    parts = path.split("/")
    if any(p in ("", ".", "..") or len(p.encode()) > 255 for p in parts):
        raise Refused(400, "bad_path")
    return parts


def claim_root(claim):
    if type(claim) is not int or not 0 <= claim < CLAIMS:
        raise Refused(400, "bad_claim")
    return os.open(os.path.join(ROOT, str(claim)), _DIR)


def parent(claim, path):
    """(fd of the parent directory, final name); every component a real directory."""
    parts = components(path)
    fd = claim_root(claim)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIR, dir_fd=fd)
            except (FileNotFoundError, NotADirectoryError):
                raise Refused(409, "parent_missing") from None
            except OSError:
                raise Refused(409, "parent_not_directory") from None
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def entry_digest(fd):
    h = hashlib.sha256()
    while block := os.read(fd, 1 << 20):
        h.update(block)
    return h.hexdigest()


def manifest(claim):
    entries, unsupported = [], []

    def walk(fd, prefix):
        with os.scandir(fd) as found:
            names = sorted(e.name for e in found)
        for name in names:
            path = prefix + name
            if len(entries) >= MAX_ENTRIES:
                raise Refused(507, "too_many_entries")
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                unsupported.append([path, "unreadable"])
                continue
            mode, owner = stat.S_IMODE(info.st_mode), [info.st_uid, info.st_gid]
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(name, _DIR, dir_fd=fd)
                except OSError:
                    unsupported.append([path, "unreadable"])
                    continue
                entries.append([path, "d", mode, *owner, 0, ""])
                try:
                    walk(child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                try:
                    child = os.open(name, _FILE, dir_fd=fd)
                except OSError:
                    unsupported.append([path, "unreadable"])
                    continue
                try:
                    entries.append([path, "f", mode, *owner, info.st_size, entry_digest(child)])
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                entries.append([path, "l", 0, *owner, 0, os.readlink(name, dir_fd=fd)])
            else:
                unsupported.append([path, "special_file"])

    root = claim_root(claim)
    try:
        walk(root, "")
    finally:
        os.close(root)
    return {"claim": claim, "entries": entries, "unsupported": unsupported,
            "files": sum(1 for e in entries if e[1] == "f"),
            "bytes": sum(e[5] for e in entries if e[1] == "f")}


def read(claim, path, offset, length):
    if not 0 <= offset or not 0 < length <= CHUNK:
        raise Refused(400, "bad_range")
    fd, name = parent(claim, path)
    try:
        try:
            file = os.open(name, _FILE, dir_fd=fd)
        except OSError:
            raise Refused(409, "unreadable") from None
        try:
            if not stat.S_ISREG(os.fstat(file).st_mode):
                raise Refused(409, "not_a_file")
            return os.pread(file, length, offset)
        finally:
            os.close(file)
    finally:
        os.close(fd)


def _remove(fd, name):
    """Remove ``name`` in directory ``fd``, a whole tree if it is a directory."""
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        child = os.open(name, _DIR, dir_fd=fd)
        try:
            with os.scandir(child) as found:
                inner = [e.name for e in found]
            for item in inner:
                _remove(child, item)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=fd)
    else:
        os.unlink(name, dir_fd=fd)


def _sync_dir(fd):
    try:
        os.fsync(fd)
    except OSError:
        pass  # some file systems cannot flush a directory; the rename stands


def write(claim, path, offset, data):
    if offset < 0 or len(data) > CHUNK:
        raise Refused(400, "bad_range")
    fd, name = parent(claim, path)
    if not name.startswith(".sova-partial-"):  # content arrives only through commit
        os.close(fd)
        raise Refused(400, "bad_partial")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        if offset == 0:
            flags |= os.O_TRUNC
        file = os.open(name, flags, 0o600, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(file).st_mode):
                raise Refused(409, "not_a_file")
            done = 0
            while done < len(data):
                done += os.pwrite(file, data[done:], offset + done)
        finally:
            os.close(file)
    finally:
        os.close(fd)


def commit(claim, partial_name, path, size, digest, mode):
    """Give the partial file ``partial_name`` (in the directory of ``path``) its
    final name once its content is exactly the expected one."""
    if components(partial_name) != [partial_name] or \
            not partial_name.startswith(".sova-partial-"):
        raise Refused(400, "bad_partial")
    fd, name = parent(claim, path)
    try:
        try:
            file = os.open(partial_name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except OSError:
            raise Refused(409, "partial_missing") from None
        try:
            info = os.fstat(file)
            if not stat.S_ISREG(info.st_mode) or info.st_size != size or \
                    entry_digest(file) != digest:
                os.close(file)
                file = None
                os.unlink(partial_name, dir_fd=fd)
                raise Refused(409, "content_changed")
            os.fchmod(file, mode)
            os.fsync(file)
        finally:
            if file is not None:
                os.close(file)
        try:
            if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                _remove(fd, name)
        except FileNotFoundError:
            pass
        os.rename(partial_name, name, src_dir_fd=fd, dst_dir_fd=fd)
        _sync_dir(fd)
    finally:
        os.close(fd)


def mkdir(claim, path, mode):
    fd, name = parent(claim, path)
    try:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(name, dir_fd=fd)
                raise FileNotFoundError
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=fd)
            _sync_dir(fd)
        child = os.open(name, _DIR, dir_fd=fd)
        try:
            os.fchmod(child, mode)
        finally:
            os.close(child)
    finally:
        os.close(fd)


def symlink(claim, path, target):
    if type(target) is not str or not target or "\0" in target or len(target) > 4096:
        raise Refused(400, "bad_target")
    fd, name = parent(claim, path)
    try:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) and os.readlink(name, dir_fd=fd) == target:
                return
            _remove(fd, name)
        except FileNotFoundError:
            pass
        os.symlink(target, name, dir_fd=fd)
        _sync_dir(fd)
    finally:
        os.close(fd)


def remove(claim, path):
    fd, name = parent(claim, path)
    try:
        _remove(fd, name)
        _sync_dir(fd)
    finally:
        os.close(fd)


def chmod(claim, path, mode):
    fd, name = parent(claim, path)
    try:
        try:
            item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except OSError:
            raise Refused(409, "unreadable") from None
        try:
            os.fchmod(item, mode)
        finally:
            os.close(item)
    finally:
        os.close(fd)


def _copy_out(directory, name, destination):
    """Copy ``name`` (never through a link) to ``destination``; False if absent."""
    try:
        source = os.open(name, _FILE, dir_fd=directory)
    except FileNotFoundError:
        return False
    except OSError:
        raise Refused(409, "unreadable") from None
    try:
        if not stat.S_ISREG(os.fstat(source).st_mode):
            raise Refused(409, "not_a_file")
        with open(destination, "wb") as out:
            while block := os.read(source, 1 << 20):
                out.write(block)
    finally:
        os.close(source)
    return True


def _value(v):
    if v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return {"f": repr(v)}
    if isinstance(v, bytes):
        return {"b": v.hex()}
    return {"r": repr(v)}


def _row_text(row):
    return json.dumps([_value(v) for v in row], ensure_ascii=False, separators=(",", ":"))


def sqlite_report(claim, path, queries):
    """A SQLite database's logical content, read from a copy in scratch space:
    the volume itself is never opened by SQLite (opening it would write a
    shared-memory index or checkpoint its log). Integrity, a digest of the
    schema, each table's rows as a multiset, and the answers to registered
    read-only queries (at most 10000 rows each)."""
    import shutil
    import sqlite3
    import tempfile

    scratch = os.environ.get("SOVA_AGENT_SCRATCH", "")
    if not scratch:
        raise Refused(409, "no_scratch")
    if type(queries) is not dict or len(queries) > 16 or not all(
            type(k) is str and type(v) is str and len(v) <= 4096 for k, v in queries.items()):
        raise Refused(400, "bad_queries")
    fd, name = parent(claim, path)
    work = tempfile.mkdtemp(dir=scratch)
    try:
        copy = os.path.join(work, "db")
        if not _copy_out(fd, name, copy):
            raise Refused(409, "missing")
        with open(copy, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise Refused(409, "not_sqlite")
        for companion in ("-wal", "-journal"):  # a log not yet checkpointed is content
            _copy_out(fd, name + companion, copy + companion)
        connection = sqlite3.connect(copy, isolation_level=None)
        try:
            return _sqlite_content(connection, queries)
        except sqlite3.DatabaseError as error:  # a file SQLite cannot read is a finding
            return {"integrity": ["unreadable: " + str(error)[:200]], "schema": None,
                    "tables": {}, "results": {}}
        finally:
            connection.close()
    finally:
        os.close(fd)
        shutil.rmtree(work, ignore_errors=True)


def _sqlite_content(connection, queries):
    """Integrity, schema digest, row digests per table and the registered
    queries' answers, from an open copy."""
    import sqlite3

    integrity = [r[0] for r in connection.execute("PRAGMA integrity_check")][:20]
    master = sorted(_row_text(r) for r in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"))
    tables = {}
    for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        quoted = '"' + table.replace('"', '""') + '"'
        texts = sorted(_row_text(r) for r in connection.execute(
            f"SELECT * FROM {quoted}"))
        digest = hashlib.sha256()
        for text in texts:
            digest.update(text.encode() + b"\n")
        tables[table] = {"rows": len(texts), "digest": digest.hexdigest()}
    connection.execute("PRAGMA query_only=ON")
    results = {}
    for key, sql in sorted(queries.items()):
        try:
            rows = connection.execute(sql).fetchmany(10001)
        except sqlite3.Error as error:
            results[key] = {"error": str(error)[:200]}
            continue
        results[key] = {"rows": [[_value(v) for v in r] for r in rows[:10000]],
                        "truncated": len(rows) > 10000}
    return {"integrity": integrity,
            "schema": hashlib.sha256("\n".join(master).encode()).hexdigest(),
            "tables": tables, "results": results}


def _int(query, key, low=0, high=1 << 62):
    try:
        value = int(query[key][0])
    except (KeyError, ValueError, IndexError):
        raise Refused(400, "bad_" + key) from None
    if not low <= value <= high:
        raise Refused(400, "bad_" + key)
    return value


def _text(query, key):
    values = query.get(key)
    if not values or len(values) != 1:
        raise Refused(400, "bad_" + key)
    return values[0]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sova-volume-agent"

    def log_message(self, *args):  # one line per request on stderr is noise here
        pass

    def _answer(self, status, body, kind="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        try:
            if url.path == "/healthz" and method == "GET":
                return self._answer(200, b'{"ok":true}')
            given = self.headers.get("X-Sova-Agent-Token", "")
            if not TOKEN or not hmac.compare_digest(
                    hashlib.sha256(given.encode()).hexdigest(), TOKEN):
                raise Refused(403, "token")
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= CHUNK:
                raise Refused(413, "body_too_large")
            body = self.rfile.read(length) if length else b""
            claim = _int(query, "claim", 0, 1 << 10) if "claim" in query else None
            if method == "GET" and url.path == "/manifest":
                return self._answer(200, json.dumps(manifest(claim)).encode())
            if method == "GET" and url.path == "/read":
                return self._answer(200, read(claim, _text(query, "path"),
                                              _int(query, "offset"), _int(query, "length")),
                                    "application/octet-stream")
            if method == "POST" and url.path == "/sqlite":
                try:
                    queries = json.loads(body or b"{}").get("queries", {})
                except (ValueError, AttributeError):
                    raise Refused(400, "bad_queries") from None
                return self._answer(200, json.dumps(sqlite_report(
                    claim, _text(query, "path"), queries)).encode())
            if MODE != "writer":
                raise Refused(405, "reader")
            if method == "PUT" and url.path == "/write":
                write(claim, _text(query, "path"), _int(query, "offset"), body)
            elif method == "POST" and url.path == "/commit":
                commit(claim, _text(query, "partial"), _text(query, "path"),
                       _int(query, "size"), _text(query, "sha256"),
                       _int(query, "mode", 0, 0o7777))
            elif method == "POST" and url.path == "/mkdir":
                mkdir(claim, _text(query, "path"), _int(query, "mode", 0, 0o7777))
            elif method == "POST" and url.path == "/symlink":
                symlink(claim, _text(query, "path"), _text(query, "target"))
            elif method == "POST" and url.path == "/remove":
                remove(claim, _text(query, "path"))
            elif method == "POST" and url.path == "/chmod":
                chmod(claim, _text(query, "path"), _int(query, "mode", 0, 0o7777))
            else:
                raise Refused(404, "unknown")
            return self._answer(200, b'{"ok":true}')
        except Refused as refused:
            return self._answer(refused.status, json.dumps({"error": refused.reason}).encode())
        except OSError as error:
            return self._answer(500, json.dumps({"error": "os", "errno": error.errno}).encode())

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_POST(self):
        self._handle("POST")


def main():
    if MODE not in ("reader", "writer") or len(TOKEN) != 64:
        sys.exit("sova volume agent: mode and token digest required")
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
