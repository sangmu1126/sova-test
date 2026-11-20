"""Real PKCE/browser, isolated native database, synthetic observation input."""

import pytest
from test_order_http_handoff import http_business as http_business  # noqa: F401

pytestmark = pytest.mark.postgres


def test_real_pkce_browser_reads_native_history(postgres_database, http_business, tmp_path):
    import hashlib
    import json
    import os
    from datetime import UTC, datetime
    from pathlib import Path
    import subprocess
    from qualification_runtime import RuntimeJourney
    from qualification_workspace_oidc import identity_provider
    from sova.adapters.postgres_change_feed import PostgresChangeFeed
    from sova.adapters.registration import registry_codec as codecs
    from sova.domain.change_impact import ChangeEvent, ChangeKind
    from sova.service.control_plane import Principal, Role

    h = RuntimeJourney(postgres_database, http_business, tmp_path)
    h.case_name = "workspace-change-history"
    artifacts = Path(os.environ["SOVA_TEST_QUALIFICATION_ARTIFACTS"])
    visible = artifacts / "change-history-browser"
    visible.mkdir(mode=0o700)
    record = h.world["contract"]
    registrar = Principal("fixture-bootstrap", h.tenant, frozenset({Role.REGISTRAR}))
    h.plane.register(registrar, "service_contract", h.app, codecs.encode(record), expected_revision=0)
    event_id = "synthetic-browser-schema-change"
    feed = PostgresChangeFeed(h.connect, tenant_id=h.tenant)
    feed.record(ChangeEvent(event_id, h.tenant, h.app, ChangeKind.SCHEMA, "synthetic-source",
                            "synthetic-db", None, datetime.now(UTC)), collector_id="fixture-collector")
    feed.record(ChangeEvent("other-app-private-event", h.tenant, "unselected-app", ChangeKind.POLICY,
                            "synthetic-source", "private-resource", None, datetime.now(UTC)),
                collector_id="fixture-collector")
    try:
        with identity_provider(h, startup_seconds=300) as idp:
            h.start_api()
            config = {"origin": h.origin, "idpOrigin": idp["origin"], "user": idp["users"]["planner"],
                      "application": h.app, "tenant": h.tenant, "event": event_id,
                      "tools": os.environ["SOVA_TEST_RECOVERY_BROWSER_TOOLS"],
                      "executable": os.environ["SOVA_TEST_RECOVERY_BROWSER_EXECUTABLE"],
                      "directory": str(visible)}
            result = subprocess.run([os.environ["SOVA_TEST_RECOVERY_BROWSER_NODE"],
                str(Path(__file__).resolve().parents[1] / "qualification_change_history_browser.mjs")],
                input=json.dumps(config), capture_output=True, text=True, timeout=120)
            if result.returncode:
                # Never persist private stdin, passwords, bearer tokens or arbitrary JS diagnostics.
                (visible / "failure.json").write_text(json.dumps({"returncode": result.returncode}))
                raise AssertionError("History browser failed; see safe phase/result artifact")
            report = json.loads(result.stdout)
            assert report["pkce"] >= 1 and report["token_exchanges"] >= 1
            assert report["history_http_status"] == 200 and report["private_app_absent"]
            assert report["logout_cleared"] and report["persistent_tokens_absent"]
            report.update(aws_vm_migration=False, operational_qualification=False,
                          fixture_events=True, fixture_inventory=True,
                          screenshot_sha256=hashlib.sha256((visible / "history.png").read_bytes()).hexdigest())
            (visible / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        h.stop_api()
