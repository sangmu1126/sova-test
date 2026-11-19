"""Real native registration UI + isolated encrypted VM control ledger.

Uses an actual disposable Keycloak (two test accounts; not enterprise SoD),
actual native HTTP API, current AWS/VM observations and PKCE. No synthetic
trust, attestation, watermark or strategy qualification is inserted. A plan
without those prerequisites must remain blocked. Business data is untouched.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

from qualification_workspace_oidc import identity_provider
from sova.adapters.registration.registry_codec import encode
from sova.application.service_contracts import example_service_contract
from sova.domain.capability_catalog import EnvironmentProfile
from sova.domain.service_contracts import ServiceContract


ROOT = Path(__file__).resolve().parents[1]
NODE = "/Users/Sooming_/.nvm/versions/node/v24.19.0/bin/node"
TOOLS = "/Users/Sooming_/dev/softbank_2026/personal-development/.local-deps/recovery-browser"
CHROME = "/Users/Sooming_/Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "control-directory", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--case", choices=("registration", "verifier", "registry"), default="registration")
    p.add_argument("--registry-package", type=Path)
    p.add_argument("--identity-startup-seconds", type=int, default=300)
    p.add_argument("--witness-installation", type=Path)
    args = p.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((args.inputs / "identity.json").read_text())
    registry_package = None
    if args.case == "registry":
        if args.registry_package is None:
            raise ValueError("An actually measured signed registry package is required")
        registry_package = json.loads(args.registry_package.read_text())
        if (registry_package["run_id"] != identity["run_id"] or registry_package["business_plan_admitted"] is not False
                or datetime.now(UTC) >= datetime.fromisoformat(registry_package["expires_at"])):
            raise ValueError("Unexpired owned measured registry package required")
    links = json.loads(args.connections.read_text())
    if identity["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"]):
        raise ValueError("Unexpired owned canary required")
    database_versions = {}
    for side in ("source", "target"):
        entry = identity["environments"][side]
        params = dict(host=entry["database_host"], hostaddr="127.0.0.1", port=links["ports"][side + "_db"],
            user="postgres", password=(args.inputs / ("client/" + side + "-admin.password")).read_text(),
            dbname=entry["database"], sslmode="verify-full", sslrootcert=str(args.inputs / "pki/ca.pem"), connect_timeout=5)
        with psycopg.connect(make_conninfo(**params)) as conn:
            name, version, system = conn.execute("SELECT current_database(),current_setting('server_version'),"
                "(SELECT system_identifier::text FROM pg_control_system())").fetchone()
        if name != side + "_orders":
            raise ValueError("Actual business database differs")
        database_versions[side] = {"version": version.split()[0], "system_identifier": system}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    tenant, application = identity["tenant_id"], identity["application_id"]
    env = {**os.environ, "SOVA_DATABASE_URL_FILE": str(args.control_directory / "runtime.dsn"),
        "SOVA_OIDC_AUDIENCE": "sova-control-plane", "SOVA_REGISTRY_VERIFIER_VERSION": "registry-attestor-1",
        "SOVA_EVIDENCE_VERIFIER": "handoff-verifier-1", "SOVA_EVIDENCE_ISSUER": "personal-handoff-producer",
        "SOVA_WITNESS": "file:" + str(args.artifacts / "witness")}
    if args.witness_installation:
        sys.path.insert(0, str(ROOT / "infra/personal-journey"))
        from witness_configuration import witness_environment

        env.update(witness_environment(args.witness_installation, identity))
    harness = SimpleNamespace(directory=args.artifacts, origin=origin, tenant=tenant,
                              environment=env, case_name="actual-personal-registration")
    report = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "database_versions": database_versions, "actual_business_mutations": False,
        "strategy_qualification_issued": False, "migration_complete": False,
        "operational_qualification": False, "identity_scope": "disposable local test IdP; not corporate SSO/MFA",
        "api_stopped": False, "identity_removed": False}
    contract = example_service_contract().document
    contract["metadata"].update(tenant_id=tenant, id=application)
    contract["spec"]["owner"] = "personal-qualification-operator"
    # A desired registered resource boundary, not a claimed geographical proof.
    for category in ("business_data", "backups", "audit"):
        contract["spec"]["residency"][category] = ["registered-personal-vm"]
    source = EnvironmentProfile("aws-ec2-b0be270e62", 1, "aws", "ec2-containerd", "postgresql",
        "iam-ssm-scram", "ssm-no-public-ingress", "arm64",
        (("postgresql", database_versions["source"]["version"]), ("containerd", "2.4.1")))
    target = EnvironmentProfile("local-rke2-b0be270e62", 1, "on_prem", "rke2-linux-vm", "postgresql",
        "ssh-kubernetes-sa-scram", "pinned-ssh-loopback", "arm64",
        (("postgresql", database_versions["target"]["version"]), ("kubernetes", "1.35.9+rke2r1")))
    registrations = [{"kind": kind, "subject": subject, "document": encode(value)} for kind, subject, value in (
        ("service_contract", application, ServiceContract.from_document(contract)),
        ("environment_profile", source.profile_id, source), ("environment_profile", target.profile_id, target))]
    api = None
    try:
        with identity_provider(harness, startup_seconds=args.identity_startup_seconds,
                               security_roles=args.case == "verifier") as idp:
            with (args.artifacts / "api.log").open("xb") as log:
                api = subprocess.Popen([sys.executable, "-m", "sova.service.main", "serve-api", "--host", "127.0.0.1", "--port", str(port)],
                    env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 25
            with httpx.Client(timeout=2, trust_env=False) as client:
                while True:
                    if api.poll() is not None:
                        raise RuntimeError("Native API stopped before readiness; inspect private log")
                    try:
                        if client.get(origin + "/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Native API did not become ready")
                    time.sleep(0.2)
            browser_input = {"origin": origin, "idpOrigin": idp["origin"], "users": idp["users"],
                "directory": str(args.artifacts), "tools": TOOLS, "executable": CHROME,
                "tenant": tenant, "application": application, "registrations": registrations,
                "source_profile": source.profile_id, "target_profile": target.profile_id}
            if registry_package is not None:
                browser_input["registrations"] = registry_package["registrations"]
                browser_input["attestations"] = registry_package["attestations"]
                report["preissued_measured_qualifications"] = 2
                report["qualified_implementation_digest"] = registry_package["implementation_digest"]
            if args.case == "verifier":
                from qualification_personal_keys import registry_proposal

                proposal = registry_proposal(identity, lambda: psycopg.connect((args.control_directory / "runtime.dsn").read_text()))
                browser_input["verifier_request"] = proposal
                report["private_key_reference"] = proposal["private_key_reference"]
            browser_script = {"verifier":"qualification_personal_verifier.mjs", "registry":"qualification_personal_registry.mjs"}.get(
                args.case, "qualification_personal_console.mjs")
            run = subprocess.run([NODE, str(ROOT / "tests" / browser_script)],
                input=json.dumps(browser_input), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            (args.artifacts / "browser.stdout").write_text(run.stdout)
            (args.artifacts / "browser.stderr").write_text(run.stderr)
            if run.returncode:
                raise RuntimeError("Actual browser registration failed; inspect private artifacts")
            report["browser"] = json.loads((args.artifacts / "browser.json").read_text())
            report["identity_runtime"] = idp["report"]
            if args.case == "verifier":
                from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry

                registry = PostgresVerifierRegistry(lambda: psycopg.connect((args.control_directory / "runtime.dsn").read_text()),
                    tenant_id=tenant, application_id=application)
                published = registry.latest("personal-registry-verifier-b0be270e62", "registry-v3")
                if (published.requested_by, published.approved_by) != (idp["users"]["planner"]["subject"], idp["users"]["approver"]["subject"]):
                    raise ValueError("Actual key publication actors differ from authenticated IdP subjects")
                report["verifier_publication"] = {"revision": published.revision, "digest": published.fingerprint,
                    "requested_by": published.requested_by, "approved_by": published.approved_by}
                report["approval_scope"] = "Distinct authenticated test subjects; not proof of two independent humans"
            with psycopg.connect((args.control_directory / "runtime.dsn").read_text()) as conn:
                conn.execute("SELECT set_config('sova.tenant_id', %s, true)", (tenant,))
                report["registered_rows"] = conn.execute("SELECT count(*) FROM sova_control.registration_heads").fetchone()[0]
                report["plan_rows"] = conn.execute("SELECT count(*) FROM sova_control.plans").fetchone()[0]
            expected_rows = 7 if args.case == "registry" else 3
            if report["registered_rows"] != expected_rows or report["plan_rows"] != 0:
                raise ValueError("Unexpected registration/plan count")
            api.terminate()
            api.wait(timeout=30)
            report["api_stopped"] = True
        report["identity_removed"] = idp["report"].get("container_removed", False)
    finally:
        if api is not None and api.poll() is None:
            api.terminate()
            api.wait(timeout=30)
        report["completed_at"] = datetime.now(UTC).isoformat()
        (args.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
