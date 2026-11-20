"""Actual readiness phases inside one disposable PKCE session.

Uses existing native registration/attestation endpoints and real isolated
strategy trials. No direct trust, evidence, plan or watermark insertion.
Incomplete measured discovery must finish with native plan refusal, not PASS.
"""

import json
import subprocess
import sys


def qualify_readiness(args, browser_input, *, root, node):
    stages = args.artifacts / "readiness"
    stages.mkdir(mode=0o700, exist_ok=False)
    result = {
        "phases": [],
        "business_plan_admitted": False,
        "migration_complete": False,
    }
    new_heads = set()

    def persist():
        (stages / "result.json").write_text(json.dumps(result, indent=2) + "\n")

    def command(name, script, arguments, timeout=300):
        item = {"name": name, "completed": False}
        result["phases"].append(item)
        persist()
        run = subprocess.run(
            [sys.executable, str(root / script), *map(str, arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        (stages / (name + ".stdout")).write_bytes(run.stdout)
        (stages / (name + ".stderr")).write_bytes(run.stderr)
        run.check_returncode()
        item["completed"] = True
        persist()

    def consume(name, path):
        package = json.loads(path.read_text())
        if (
            package["run_id"] != "b0be270e62"
            or package["business_plan_admitted"] is not False
        ):
            raise ValueError(
                "Only actual unadmitted personal registry packages allowed"
            )
        directory = stages / name
        directory.mkdir(mode=0o700, exist_ok=False)
        data = {
            **browser_input,
            "directory": str(directory),
            "registrations": package["registrations"],
            "attestations": package["attestations"],
        }
        run = subprocess.run(
            [node, str(root / "tests/qualification_personal_registry.mjs")],
            input=json.dumps(data),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        (directory / "browser.stdout").write_text(run.stdout)
        (directory / "browser.stderr").write_text(run.stderr)
        run.check_returncode()
        browser = json.loads((directory / "browser.json").read_text())
        result["phases"].append({"name": name, "completed": True, "browser": browser})
        new_heads.update((r["kind"], r["subject"]) for r in package["registrations"])
        persist()
        return browser

    common = ["--inputs", args.inputs, "--control-directory", args.control_directory]
    contract = stages / "contract-package.json"
    command(
        "contract-proposal",
        "infra/personal-journey/renew_contract_attestation.py",
        [*common, "--output", contract],
    )
    consume("contract-ingestion", contract)
    trial = stages / "strategy-trial"
    command(
        "directional-trial",
        "tests/qualification_personal_strategy.py",
        [
            "--inputs",
            args.inputs,
            "--connections",
            args.connections,
            "--artifacts",
            trial,
        ],
        timeout=900,
    )
    strategies = stages / "strategy-package.json"
    command(
        "strategy-proposal",
        "infra/personal-journey/qualify_strategy.py",
        [
            *common,
            "--connections",
            args.connections,
            "--trial",
            trial / "trial.json",
            "--output",
            strategies,
        ],
    )
    consume("strategy-ingestion", strategies)
    facts = stages / "dependency-facts"
    command(
        "dependency-measurement",
        "infra/personal-journey/collect_dependency_facts.py",
        [
            "--inputs",
            args.inputs,
            "--connections",
            args.connections,
            "--artifacts",
            facts,
        ],
    )
    inventory = stages / "dependency-package.json"
    command(
        "dependency-proposal",
        "infra/personal-journey/measured_dependency_snapshot.py",
        [*common, "--facts", facts / "facts.json", "--output", inventory],
    )
    measured = json.loads(inventory.read_text())
    if measured.get("partial_inventory") is not True:
        raise ValueError(
            "This bounded collector never proves complete writer/controller coverage"
        )
    browser = consume("dependency-ingestion-and-refusal", inventory)
    if browser.get("compile_refusal", {}).get("http_status") != 422:
        raise ValueError("Actual incomplete discovery must not admit a plan")
    result.update(actual_partial_inventory_refused=True, complete=True)
    persist()
    return new_heads, result
