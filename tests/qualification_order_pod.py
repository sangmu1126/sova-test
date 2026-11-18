"""Test-only HTTPS launcher for the existing order application, fixed to one DB.

Mounted only in the disposable business Pods, never overlaid on the worker.
No worker credentials, catalog, admin DSN, or Kubernetes API token is mounted.
"""

import json
from pathlib import Path
import sys

import psycopg
import uvicorn
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent))
from qualification_orders import application  # noqa: E402


def main():
    directory = Path("/etc/order-fixture")
    config = json.loads((directory / "config.json").read_text())
    dsn = (directory / "dsn").read_text().strip()
    database, role, side = config["database"], config["role"], config["side"]

    class FixedDatabase:
        def get(self):
            return database

    def connect_as(wanted_database, wanted_role):
        if (wanted_database, wanted_role) != (database, role):
            raise ValueError("Fixture database identity mismatch")
        return psycopg.connect(dsn, dbname=database, connect_timeout=2)

    app = application({"connect_as": connect_as, "app": role}, FixedDatabase(),
                      config["scope"], (directory / "token").read_text().strip(),
                      lookup_retry_after_seconds=config.get("lookup_retry_after_seconds"))

    @app.middleware("http")
    async def side_marker(request, call_next):
        if request.url.path == "/healthz":
            return JSONResponse({"status": "ok", "side": side})
        response = await call_next(request)
        response.headers["x-qualification-side"] = side
        return response

    uvicorn.run(app, host="0.0.0.0", port=8443, access_log=False, log_level="warning",
                proxy_headers=False, ssl_certfile=str(directory / "server.pem"),
                ssl_keyfile=str(directory / "server-key.pem"), timeout_keep_alive=2)


if __name__ == "__main__":
    main()
