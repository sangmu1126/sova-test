"""Private, value-free diagnostics for isolated qualification fixtures only."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import traceback

from sova.adapters.postgres_plans import PostgresLifecycleRepository


class ExceptionDiagnostics:
    def __init__(self, app, path):
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except Exception as error:
            # No exception message, locals, URL, request body or credentials.
            record = {"at": datetime.now(UTC).isoformat(), "exception_class": type(error).__name__,
                      "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                                 for f in traceback.extract_tb(error.__traceback__)]}
            with self.path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            raise


def capture_state(h, env):
    """Retain safe-hold reasons/failed obligations without observation bodies."""
    try:
        repo = PostgresLifecycleRepository(h.connect, tenant_id=h.tenant)
        with repo.transaction() as connection:
            authority = connection.execute(
                "SELECT state,revision,document->>'reason',document->>'next_operation' "
                "FROM sova_control.handoff_authority WHERE application_id=%s", (h.app,)).fetchall()
            effects = connection.execute(
                "SELECT command_document->>'action',status FROM sova_control.effects WHERE application_id=%s",
                (h.app,)).fetchall()
            evidence = connection.execute(
                "SELECT document #>> '{evidence,obligation_id}',document #>> '{evidence,result}' "
                "FROM sova_control.signed_security_evidence WHERE application_id=%s", (h.app,)).fetchall()
        env.report["final_sql_diagnostics"] = {"authority": authority, "effects": effects,
                                                "evidence_results": evidence}
    except Exception as error:
        env.report["final_sql_diagnostics_error"] = type(error).__name__
    env.save()
