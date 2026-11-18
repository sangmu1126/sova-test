from types import SimpleNamespace
import json

from fastapi import FastAPI
from handoff_support import admitted_handoff
import httpx
import pytest
from qualification_diagnostics import ExceptionDiagnostics, capture_state
from qualification_orders import serve


def test_real_tcp_500_records_class_and_frames_without_private_values(tmp_path):
    app = FastAPI()
    @app.get("/test")
    def failure():
        raise RuntimeError("secret-password-and-dsn-must-not-be-recorded")
    path = tmp_path / "exceptions.jsonl"
    with serve(ExceptionDiagnostics(app, path)) as origin:
        response = httpx.get(origin + "/test", trust_env=False)
        assert response.status_code == 500
    value = json.loads(path.read_text())
    assert value["exception_class"] == "RuntimeError" and value["frames"]
    assert "secret-password-and-dsn-must-not-be-recorded" not in path.read_text()
    assert all(set(f) == {"file", "function", "line"} for f in value["frames"])


@pytest.mark.postgres
def test_final_diagnostics_can_read_real_tenant_scoped_tables(postgres_database, now):
    h = admitted_handoff(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="writer")
    env = SimpleNamespace(report={}, save=lambda: None)
    capture_state(SimpleNamespace(connect=h["connect"], tenant=h["tenant"], app=h["app"]), env)
    assert "final_sql_diagnostics_error" not in env.report
    value = env.report["final_sql_diagnostics"]
    assert value["authority"][0][:2] == ("SOURCE_ACTIVE", 1)
    assert set(value) == {"authority", "effects", "evidence_results"}
