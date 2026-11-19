"""Execution configuration v2: exact compiled direction and measured source bytes.

This reuses v1's real adapter factories. It does not infer a reverse database,
credential or probe from a forward configuration. Both directions must be
registered explicitly, and an unregistered pair is refused before any factory
is called. A source-code fingerprint is a host-profile integrity check, not
hardware attestation or protection against a compromised OS/administrator.
"""

import hashlib
import json
from pathlib import Path

from sova.domain.contracts import ContractError, canonical_json, digest, identifier
from sova.domain.plan_compiler import PlanRevision
from sova.service.worker import AdapterCatalog

PACKAGE = Path(__file__).resolve().parents[1]


def source_identity(root=PACKAGE):
    """Every shipped Python/SQL file; only internal SQL compatibility links.

    The package is copied into a run-owned, read-only worker snapshot before
    launch. Caller supplied directories are only useful for build/test tooling;
    the running catalog always measures its own installed package.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ContractError("Real implementation package directory required")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if (path.suffix != ".sql" or not path.resolve().is_relative_to(root.resolve())
                    or not path.resolve().is_file()):
                raise ContractError("Implementation source may not contain external or executable symlinks")
        if path.is_file() and path.suffix in {".py", ".sql"}:
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if len(files) > 4096:
            raise ContractError("Implementation package exceeds its source bound")
    if not files:
        raise ContractError("Implementation source files required")
    document = {"version": 1, "files": files}
    return {**document, "digest": hashlib.sha256(canonical_json(document, limit=1 << 20).encode()).hexdigest()}


class DirectionalCatalog(AdapterCatalog):
    def __init__(self, entries, *, expected_digest, measure=source_identity):
        digest(expected_digest)
        if not callable(measure) or type(entries) is not dict or not entries:
            raise ContractError("Registered directional factories and implementation measurement required")
        super().__init__({})
        self.entries, self.expected_digest, self.measure = dict(entries), expected_digest, measure
        self._verify_bytes()

    def _verify_bytes(self):
        if self.measure()["digest"] != self.expected_digest:
            raise ContractError("Worker implementation differs from its registered source identity")

    def verify_execution(self, tenant_id, application_id, adapter_id, revision):
        if not isinstance(revision, PlanRevision):
            raise ContractError("Frozen compiled plan required for direction selection")
        document = json.loads(revision.canonical)
        if (document.get("tenant_id"), document.get("service_id")) != (tenant_id, application_id):
            raise ContractError("Compiled plan belongs to another application")
        pair = document.get("environments")
        if type(pair) is not dict or set(pair) != {"source", "target"}:
            raise ContractError("Exact compiled source and target required")
        for side in ("source", "target"):
            identifier(pair[side])
        key = tenant_id, application_id, adapter_id, pair["source"], pair["target"]
        factory = self.entries.get(key)
        if factory is None:
            raise ContractError("No execution registered for this application and direction")
        adapters = [op.get("adapter") for op in document.get("operations", ()) if op.get("adapter")]
        if not adapters or any(a.get("adapter_id") != adapter_id or a.get("adapter_digest") != self.expected_digest for a in adapters):
            raise ContractError("Plan adapter identity differs from the running implementation")
        self._verify_bytes()  # Check again before a factory can construct effects/probes.
        return factory

    def build(self, tenant_id, application_id, adapter_id, revision):
        factory = self.verify_execution(tenant_id, application_id, adapter_id, revision)
        return factory(tenant_id, application_id, adapter_id, revision)


def catalog_v2(document, *, catalog_builder=None):
    from sova.service.catalog import catalog_from

    if type(document) is not dict or set(document) != {"version", "implementation_digest", "applications"} or document["version"] != 2:
        raise ContractError("Directional execution configuration version 2 required")
    digest(document["implementation_digest"])
    apps = document["applications"]
    if type(apps) is not list or not 1 <= len(apps) <= 128:
        raise ContractError("Bounded directional application entries required")
    build = catalog_builder or catalog_from
    entries, closes = {}, []
    try:
        for item in apps:
            if type(item) is not dict:
                raise ContractError("Application execution object required")
            app = dict(item)
            pair = app.pop("environment_pair", None)
            if type(pair) is not dict or set(pair) != {"source", "target"} or pair["source"] == pair["target"]:
                raise ContractError("Distinct registered source and target environments required")
            for value in (app.get("tenant_id"), app.get("application_id"), app.get("adapter_id"), *pair.values()):
                identifier(value)
            key = app["tenant_id"], app["application_id"], app["adapter_id"], pair["source"], pair["target"]
            if key in entries:
                raise ContractError("Execution direction registered twice")
            inner, close = build({"version": 1, "applications": [app]})
            closes.append(close)
            entries[key] = inner.build
        catalog = DirectionalCatalog(entries, expected_digest=document["implementation_digest"])
    except BaseException:
        for close in closes:
            close()
        raise

    def close():
        for end in closes:
            end()

    return catalog, close
