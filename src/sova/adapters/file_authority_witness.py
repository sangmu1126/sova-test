"""Hash-chained, append-only authority witness on a local file (AT-33, §15.6).

Every authority record the store commits is also appended here, inside the
store's transaction and before its commit. If the authority database is later
restored to an earlier point, its current row no longer matches the latest
witnessed record and progress stops until a recovery incarnation.

Each line carries the SHA-256 of the previous line, so editing or reordering
earlier entries is detected on read. A file cannot prove that its tail was
not cut off; production deployments put the witness on storage with its own
retention lock (for example object storage with WORM retention) under a
separate permission boundary from the authority database. The port is::

    append(application_id, unit_digest, action, document) -> None
    latest(application_id, unit_digest) -> dict | None
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading

from sova.domain.contracts import ContractError, canonical_json, digest, identifier

try:
    import fcntl
except ImportError:  # No thread-only fallback on an unsupported platform.
    fcntl = None

_GENESIS = "0" * 64


class FileAuthorityWitness:
    def __init__(self, path, *, tenant_id):
        self.path = Path(path)
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self._lock = threading.Lock()

    @contextmanager
    def _file(self, *, write):
        if fcntl is None:
            raise ContractError("Local authority witness requires POSIX file locking")
        with self._lock:
            try:
                handle = self.path.open("a+" if write else "r", encoding="utf-8")
            except FileNotFoundError:
                if write:
                    raise
                yield None
                return
            with handle:
                fcntl.flock(handle, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                try:
                    yield handle
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _entries(self, handle):
        entries, previous = [], _GENESIS
        if handle is None:
            return entries, previous
        handle.seek(0)
        for line in handle:
            if not line.endswith("\n"):
                raise ContractError("Authority witness record is incomplete")
            line = line.rstrip("\n")
            try:
                entry = json.loads(line)
            except ValueError:
                raise ContractError("Authority witness record is invalid") from None
            if (type(entry) is not dict or set(entry) != {
                    "tenant_id", "application_id", "unit_digest", "action", "record", "prev"}
                    or type(entry["record"]) is not dict):
                raise ContractError("Authority witness record is invalid")
            identifier(entry["application_id"])
            identifier(entry["action"], "action", limit=512)
            digest(entry["unit_digest"])
            if entry["prev"] != previous or entry["tenant_id"] != self.tenant_id:
                raise ContractError("Authority witness chain is broken")
            previous = hashlib.sha256(line.encode()).hexdigest()
            entries.append(entry)
        return entries, previous

    def append(self, application_id, unit_digest, action, document):
        identifier(application_id)
        digest(unit_digest)
        identifier(action, "action", limit=512)
        if type(document) is not dict:
            raise ContractError("Authority record document required")
        with self._file(write=True) as handle:
            _, previous = self._entries(handle)
            line = canonical_json({
                "tenant_id": self.tenant_id, "application_id": application_id,
                "unit_digest": unit_digest, "action": action, "record": document,
                "prev": previous,
            })
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def latest(self, application_id, unit_digest):
        with self._file(write=False) as handle:
            entries, _ = self._entries(handle)
            found = [e["record"] for e in entries
                     if (e["application_id"], e["unit_digest"]) == (application_id,
                                                                     unit_digest)]
        return found[-1] if found else None
