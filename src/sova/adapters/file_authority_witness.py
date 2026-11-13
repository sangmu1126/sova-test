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

import hashlib
import json
import os
from pathlib import Path
import threading

from sova.domain.contracts import ContractError, canonical_json, digest, identifier

_GENESIS = "0" * 64


class FileAuthorityWitness:
    def __init__(self, path, *, tenant_id):
        self.path = Path(path)
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self._lock = threading.Lock()

    def _entries(self):
        if not self.path.exists():
            return []
        entries, previous = [], _GENESIS
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                entry = json.loads(line)
                if entry.get("prev") != previous or entry.get("tenant_id") != self.tenant_id:
                    raise ContractError("Authority witness chain is broken")
                previous = hashlib.sha256(line.encode()).hexdigest()
                entries.append(entry)
        return entries

    def append(self, application_id, unit_digest, action, document):
        identifier(application_id)
        digest(unit_digest)
        identifier(action, "action", limit=512)
        if type(document) is not dict:
            raise ContractError("Authority record document required")
        with self._lock:
            previous = _GENESIS
            if self.path.exists():
                entries = self._entries()
                if entries:
                    with self.path.open("rb") as handle:
                        last = handle.read().rstrip(b"\n").rsplit(b"\n", 1)[-1]
                    previous = hashlib.sha256(last).hexdigest()
            line = canonical_json({
                "tenant_id": self.tenant_id, "application_id": application_id,
                "unit_digest": unit_digest, "action": action, "record": document,
                "prev": previous,
            })
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def latest(self, application_id, unit_digest):
        with self._lock:
            found = [e["record"] for e in self._entries()
                     if (e["application_id"], e["unit_digest"]) == (application_id,
                                                                     unit_digest)]
        return found[-1] if found else None
