"""Fixed-database runtime for the scoped, disposable HTTP/PG order canary.

The workload has no migration/admin endpoint, cloud credential, route writer
or database administrator credential. This is a qualification workload, not a
customer authentication, payment or inventory product.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import stat

from fastapi.responses import JSONResponse
import psycopg
from psycopg.conninfo import conninfo_to_dict

from sova.qualification.order_app import application
from sova.qualification.requests import identifier


def _file(root, name, *, public=False):
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Canary configuration escaped its fixed mount")
    with path.open("rb") as file:
        metadata = os.fstat(file.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or metadata.st_mode & (0o027 if not public else 0o022)):
            raise ValueError("Canary configuration ownership or permissions are unsafe")
        data = file.read(8193)
    if not data or len(data) > 8192:
        raise ValueError("Bounded nonempty canary configuration required")
    return data.decode("utf-8").strip()


@dataclass(frozen=True)
class Configuration:
    tenant: str
    application: str
    run: str
    side: str
    image_digest: str
    role: str
    database: str
    dsn: str
    token: str
    expires_at: datetime


def load_configuration(root=Path("/run/sova"), *, now=None):
    root = Path(root)
    document = json.loads(_file(root, "config.json"))
    if type(document) is not dict or set(document) != {
        "version", "tenant_id", "application_id", "run_id", "side", "deployment_digest", "expires_at"
    } or type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("Canary configuration schema mismatch")
    for key in ("tenant_id", "application_id", "run_id"):
        identifier(document[key])
    if document["side"] not in ("source", "target") or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(document["deployment_digest"])):
        raise ValueError("Fixed side and OCI digest required")
    expires = datetime.fromisoformat(document["expires_at"])
    if expires.tzinfo is None:
        raise ValueError("Timezone-aware canary deadline required")
    at = now or datetime.now(UTC)
    if not at < expires <= at + timedelta(hours=8):
        raise ValueError("Canary deadline must be within eight hours")
    dsn = _file(root, "database.dsn")
    try:
        parts = conninfo_to_dict(dsn)
    except psycopg.Error:
        raise ValueError("Invalid canary database configuration") from None
    role, database = parts.get("user"), parts.get("dbname")
    identifier(role)
    identifier(database)
    if role in {"postgres", "root", "sova_runtime"} or parts.get("sslmode") != "verify-full":
        raise ValueError("Non-admin role and verified PostgreSQL TLS required")
    if (not parts.get("host") or not parts.get("password") or parts.get("service")
            or parts.get("passfile") or parts.get("sslcert") or parts.get("sslkey")
            or not 1 <= int(parts.get("connect_timeout", "0")) <= 10):
        raise ValueError("Fixed host, password and bounded TLS connection required")
    if any("," in parts.get(field, "") or any(c.isspace() for c in parts.get(field, ""))
           for field in ("host", "hostaddr", "port")):
        raise ValueError("A single registered database endpoint is required")
    expected_ca = root / "database-ca.pem"
    if parts.get("sslrootcert") != str(expected_ca):
        raise ValueError("Only the mounted database CA is accepted")
    _file(root, "database-ca.pem", public=True)
    token = _file(root, "client.token")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Bounded run client credential required")
    return Configuration(document["tenant_id"], document["application_id"], document["run_id"],
        document["side"], document["deployment_digest"], role, database, dsn, token, expires)


class FixedDatabase:
    def __init__(self, database):
        self.database = database

    def get(self):
        return self.database


def create_app(root=Path("/run/sova")):
    config = load_configuration(root)

    def connect_as(database, role):
        if (database, role) != (config.database, config.role):
            raise ValueError("Unregistered canary database or role")
        conn = psycopg.connect(config.dsn, application_name="sova-canary-" + config.run,
            options="-c statement_timeout=2000 -c lock_timeout=500")
        try:
            privileges = conn.execute("SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,"
                "EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) "
                "FROM pg_roles WHERE rolname=current_user").fetchone()
            if privileges is None or any(privileges):
                raise psycopg.OperationalError("Canary role has unapproved server privileges")
            return conn
        except BaseException:
            conn.close()
            raise

    app = application({"connect_as": connect_as, "app": config.role},
        FixedDatabase(config.database), {"tenant_id": config.tenant, "run_id": config.run}, config.token)

    @app.middleware("http")
    async def identity_and_expiry(request, call_next):
        if datetime.now(UTC) >= config.expires_at:
            response = JSONResponse({"error": "canary registration expired"}, status_code=503)
        elif request.method == "GET" and request.url.path == "/healthz":
            # Liveness/side identity only. SQL readiness and integrity are
            # independently probed; no database credential is used here.
            response = JSONResponse({"application_id": config.application, "side": config.side})
        else:
            response = await call_next(request)
        response.headers["x-sova-side"] = config.side
        response.headers["x-sova-deployment"] = config.image_digest
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    return app
