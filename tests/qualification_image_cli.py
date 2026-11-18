"""Run installed worker startup rejection cases in an immutable local image.

These are negative CLI acceptance tests, not a healthy worker/Pod or migration
qualification. Only generated, deliberately unusable fixture secrets are
mounted. No source overlay, database, Kubernetes API or shared cluster is used.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from sova.qualification.image import inspect_image


CASES = (
    ("invalid-health-port", ["--health-port", "0"], {},
     "Worker health_port must be an integer in 1..65535"),
    ("invalid-lease", [], {"SOVA_LEASE_SECONDS": "301"},
     "SOVA_LEASE_SECONDS is outside execution ledger bounds"),
    ("invalid-poll", ["--poll-seconds", "nan"], {},
     "Worker poll_seconds must be positive and below the stall timeout"),
    ("empty-catalog", [], {}, "At least one application is required"),
)


def accepted_rejection(returncode, output, expected):
    if returncode != 1:
        return False
    try:
        logs = [json.loads(line) for line in output.splitlines()]
    except ValueError:
        return False
    if not all(type(line) is dict for line in logs):
        return False
    errors = [line for line in logs if line.get("level") == "ERROR"]
    exception = errors[0].get("exc", "") if len(errors) == 1 else ""
    return (len(errors) == 1 and errors[0].get("msg") == "run-worker failed"
            and type(exception) is str and bool(exception.splitlines())
            and exception.splitlines()[-1].endswith("ContractError: " + expected)
            and not any("serving" in line.get("msg", "") for line in logs))


def fixture_files(directory):
    files = {"database": "host=unreachable.invalid user=not-a-runtime-role\n",
             "key": "11" * 32,
             "execution": json.dumps({"version": 1, "applications": []})}
    for name, text in files.items():
        path = directory / name
        path.write_text(text)
        # The unprivileged image user must be able to read these dummy fixture
        # values. They are not deployment credentials or secret exports.
        path.chmod(0o444)
    return {
        "SOVA_DATABASE_URL_FILE": "/run/fixture/database",
        "SOVA_OIDC_ISSUER": "https://unreachable.invalid/realm",
        "SOVA_OIDC_AUDIENCE": "startup-fixture",
        "SOVA_OIDC_JWKS_URL": "https://unreachable.invalid/keys",
        "SOVA_REGISTRY_VERIFIER_VERSION": "fixture",
        "SOVA_EVIDENCE_VERIFIER": "fixture",
        "SOVA_EVIDENCE_ISSUER": "fixture",
        "SOVA_WITNESS": "file:/unwritable-fixture",
        "SOVA_TENANTS": "fixture",
        "SOVA_EVIDENCE_KEY_FILE": "/run/fixture/key",
        "SOVA_EVIDENCE_KEY_ID": "fixture",
        "SOVA_EXECUTION_FILE": "/run/fixture/execution",
        # Formatting inputs only. No fake Pod-state observer or quiescence
        # proof is installed, requested or produced by these rejection cases.
        "POD_NAME": "not-an-actual-pod", "POD_UID": str(uuid4()),
        "POD_NAMESPACE": "not-an-actual-namespace",
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--architecture", required=True, choices=("arm64", "amd64"))
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    identity = inspect_image(args.image, args.architecture)
    args.artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"schema_version": "worker-image-startup.v1", "observed_at": datetime.now(UTC).isoformat(),
              **identity, "qualification_issued": False, "authorizes_execution": False,
              "pod_observed": False, "healthy_worker_observed": False,
              "source_overlay": False, "network": "none", "cases": []}
    with tempfile.TemporaryDirectory(prefix="sova-cli-fixtures-") as tmp:
        fixtures = Path(tmp)
        fixtures.chmod(0o755)
        base = fixture_files(fixtures)
        for case, arguments, changes, expected in CASES:
            name = "sova-image-cli-" + uuid4().hex
            command = ["docker", "run", "--rm", "--pull", "never", "--name", name,
                       "--label", "sova.image-cli=" + name, "--network", "none", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--pids-limit", "32", "--memory", "256m", "--cpus", "1",
                       "--user", "10001:10001", "--mount",
                       f"type=bind,source={fixtures},target=/run/fixture,readonly"]
            for key, value in {**base, **changes}.items():
                command.extend(("--env", key + "=" + value))
            # Use the image's actual ENTRYPOINT, not Python source injection.
            command.extend((args.image, "run-worker", *arguments))
            output, code, error = "", None, None
            try:
                run = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, timeout=45)
                output, code = run.stdout, run.returncode
            except subprocess.TimeoutExpired:
                error = "worker_did_not_refuse_startup_within_timeout"
            finally:
                found = subprocess.run(["docker", "inspect", "--format",
                                        '{{index .Config.Labels "sova.image-cli"}}', name],
                                       capture_output=True, text=True, timeout=20)
                if found.returncode == 0 and found.stdout.strip() == name:
                    subprocess.run(["docker", "rm", "--force", name], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            log = args.artifacts / (case + ".jsonl")
            with log.open("x") as stream:
                stream.write(output)
            log.chmod(0o600)
            report["cases"].append({"name": case, "exit_code": code, "error": error,
                                     "expected_contract_error": expected,
                                     "passed": accepted_rejection(code, output, expected),
                                     "log_sha256": hashlib.sha256(output.encode()).hexdigest()})
    report["passed"] = all(case["passed"] for case in report["cases"])
    path = args.artifacts / "run.json"
    with path.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]),
                      "qualification_issued": False}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
