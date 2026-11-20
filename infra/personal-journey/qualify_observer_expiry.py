"""Observe real deadline enforcement in an existing authenticated TLS session.

No clock changes, role changes, credential renewal or committed probe writes.
The session starts before the existing trial deadline. Restrictive RLS must
hide observations and refuse appends after expiry even though login/TLS already
succeeded. Password VALID UNTIL alone does not terminate existing sessions.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict

from sova.adapters.kubernetes.https import private_text


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "collector-directory", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((a.inputs / "identity.json").read_text())
    until = datetime.fromisoformat(identity["expires_at"])
    left = (until - datetime.now(UTC)).total_seconds()
    if identity["run_id"] != "b0be270e62" or not 10 < left <= 1200:
        raise ValueError("Existing owned deadline within twenty minutes required")
    dsn = private_text(a.collector_directory / "observer.dsn")
    settings = conninfo_to_dict(dsn)
    if settings.get("user") != "sj_observe_b0be270e62" or settings.get("dbname") != "sj_control_b0be270e62" or settings.get("sslmode") != "verify-full":
        raise ValueError("Exact observation-only TLS credential required")
    started_code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = {"version": 1, "started_at": datetime.now(UTC).isoformat(),
              "expires_at": until.isoformat(), "role": settings["user"],
              "existing_tls_session": True, "clock_changed": False,
              "committed_probe_writes": 0, "samples": [], "checks": {},
              "migration_complete": False}
    output = a.artifacts / "result.json"

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    save()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                         options="-c statement_timeout=5000 -c lock_timeout=1000") as c:
        row = c.execute("SELECT pg_backend_pid(),clock_timestamp(),(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),(SELECT rolvaliduntil FROM pg_roles WHERE rolname=current_user)").fetchone()
        if row[1] >= until or row[2] is not True or row[3] != until:
            raise ValueError("Existing live TLS session and unchanged deadline required")
        pid = row[0]
        c.execute("SELECT set_config('sova.tenant_id',%s,false)", (identity["tenant_id"],))
        before = c.execute("SELECT tenant_id,application_id,kind,environment_id,subject,observed_at,collector_id FROM sova_control.change_events WHERE collector_id='personal-durable-cloudtrail-v2' ORDER BY observed_at LIMIT 1").fetchone()
        if before is None:
            raise ValueError("Actual prior CloudTrail observation is the positive control")
        report["checks"]["actual_observation_readable_before_expiry"] = True
        timeout = time.monotonic() + left + 20
        while True:
            current, backend = c.execute("SELECT clock_timestamp(),pg_backend_pid()").fetchone()
            report["samples"].append({"host_at": datetime.now(UTC).isoformat(),
                                      "database_at": current.isoformat(),
                                      "same_authenticated_session": backend == pid})
            save()
            if backend != pid:
                raise ValueError("Session was replaced; do not count as an existing-session test")
            if current >= until:
                break
            if time.monotonic() >= timeout:
                raise ValueError("Observer deadline did not arrive within the fixed budget")
            time.sleep(min(10, max(0.05, (until - current).total_seconds() + 0.05)))
        report["checks"]["same_session_after_database_deadline"] = True
        report["checks"]["prior_observations_hidden_after_expiry"] = c.execute("SELECT count(*) FROM sova_control.change_events").fetchone()[0] == 0
        refused = False
        try:
            with c.transaction(force_rollback=True):
                c.execute("INSERT INTO sova_control.change_events(tenant_id,event_id,application_id,kind,environment_id,subject,observed_at,collector_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                          (before[0], "expiry-probe-" + uuid4().hex, *before[1:]))
        except psycopg.errors.InsufficientPrivilege:
            refused = True
        report["checks"]["matching_scope_append_denied_after_expiry"] = refused
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["driver_sha256"] = started_code
    report["checks"]["driver_unchanged"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == started_code
    report["passed"] = all(report["checks"].values())
    save()
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]),
                      "existing_tls_session": True, "committed_probe_writes": 0}))
    if not report["passed"]:
        raise ValueError("Existing-session expiry test failed; preserve the result")


if __name__ == "__main__":
    main()
