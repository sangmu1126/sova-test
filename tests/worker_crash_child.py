"""A worker process that dies in the middle of one step (test helper).

Run as ``python worker_crash_child.py '<json spec>'``: it builds a worker
exactly as a deployment would, wraps the adapter so that, right after the
step named ``crash_after`` has made its change and before its readback is
recorded, the process exits with status 17 - a worker lost mid-step.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from postgres_business import PROBE_INSERT, SCHEMA, TABLES, Route  # noqa: E402
import psycopg  # noqa: E402

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner  # noqa: E402
from sova.adapters.file_authority_witness import FileAuthorityWitness  # noqa: E402
from sova.service.control_plane import Settings  # noqa: E402
from sova.service.postgres_execution import postgres_factory  # noqa: E402
from sova.service.worker import AdapterCatalog, Execution, HandoffWorker  # noqa: E402


class DiesAfterApply:
    def __init__(self, inner, action):
        self.inner, self.action, self.adapter_id = inner, action, inner.adapter_id

    def validate(self, command):
        self.inner.validate(command)

    def apply(self, command):
        self.inner.apply(command)
        if command.action == self.action:
            os._exit(17)  # the change is made; the worker never hears back

    def readback(self, command, now):
        return self.inner.readback(command, now)


def main(spec):
    names, local = spec["names"], spec["local_dsn"]

    def resolve(tenant_id, application_id):
        return {"connect": lambda db: psycopg.connect(local, dbname=db),
                "connect_as": lambda db, role: psycopg.connect(local, dbname=db, user=role),
                "source_db": names["src"], "target_db": names["tgt"],
                "scratch_db": names["scratch"], "other_db": names["other"],
                "app_role": spec["app_role"], "writer_roles": (spec["app_role"],
                                                               spec["batch_role"]),
                "tables": TABLES, "schema_sql": SCHEMA, "probe_insert": PROBE_INSERT,
                "route": Route(names["src"])}

    inner = postgres_factory(spec["adapter_id"], resolve)

    def crashing(tenant_id, application_id, revision):
        execution = inner(tenant_id, application_id, revision)
        return Execution(DiesAfterApply(execution.adapter, spec["crash_after"]),
                         execution.measure)

    settings = Settings(**spec["settings"], witness_for=lambda t: FileAuthorityWitness(
        spec["witness"], tenant_id=t))
    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(spec["key"])),
        issuer_id="handoff-producer", key_id="k1")
    worker = HandoffWorker(lambda: psycopg.connect(spec["dsn"], user=spec["role"]),
                           settings=settings,
                           catalog=AdapterCatalog({spec["adapter_id"]: crashing}),
                           signer=signer, clock=lambda: datetime.now(UTC),
                           owner_id=spec["owner"])
    worker.drive(spec["tenant"], spec["application"], spec["unit"])
    sys.exit(3)  # reaching here means the crash point was never hit


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
