"""Trusted personal verifier: package measured directional adapter qualifications.

Run privately as the canary verifier, never as an uploaded-artifact endpoint or
an AI tool. Rechecks current code, registered profiles, real DB identity/TLS,
bounded measured rows and the already independently approved Keychain key.
Produces public registrations/signatures for native ingestion; never registers,
approves, admits or executes a business plan. LIVE denotes this actual AWS/VM
test, not a continuity, financial-compliance, HA or arbitrary-app qualification.
"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import psycopg
from psycopg.conninfo import make_conninfo

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.postgres_registrations import PostgresRegistrations
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.registration.registry_codec import encode, subject_of
from sova.domain.capability_catalog import CapabilityManifest, StrategyQualification, EvidenceGrade
from sova.domain.contracts import canonical_json
from sova.domain.plan_compiler import required_operations, FAILBACK_STRATEGY
from sova.domain.registry_attestation import attestation_record
from sova.domain.service_contracts import MigrationProfile
from sova.service.directional_catalog import source_identity

ROOT = Path(__file__).resolve().parents[2]
ISSUER, KEY_ID, VERIFIER = "personal-registry-verifier-b0be270e62", "registry-v3", "registry-attestor-1"
ACTIONS = {"stage_target":"stage_target","verify_return_target":"verify_return_target",
    "start_sync":"start_sync","pause_admission":"pause_admission","fence_writes":"fence_source_writes",
    "copy_content":"copy_content","apply_final_changes":"apply_final_changes","verify_content":"verify_final_state",
    "activate_writer":"activate_target","commit_route":"commit_route"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "trial", "control-directory", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError("Preserve prior qualification packages")
    identity = json.loads((args.inputs/"identity.json").read_text())
    links = json.loads(args.connections.read_text())
    raw = args.trial.read_bytes()
    trial = json.loads(raw)
    now = datetime.now(UTC)
    until = datetime.fromisoformat(identity["expires_at"])
    started, completed = (datetime.fromisoformat(trial[key]) for key in ("started_at","completed_at"))
    if (started.tzinfo is None or completed.tzinfo is None or not started<=completed<=now
            or now-completed>timedelta(minutes=30)):
        raise ValueError("Recent completed directional trial required; never accept future or stale measurements")
    required_checks = {"nologin_is_not_activation","forward_content_and_sequence_equal",
        "new_target_write_preserved_on_failback","single_writer_after_failback","actual_route_returned_to_source",
        "native_and_security_implementation_unchanged","original_app_data_privileges_and_entry_unchanged"}
    if (identity["run_id"]!="b0be270e62" or now>=until or trial.get("passed") is not True
            or set(trial.get("checks",{}))!=required_checks
            or not all(value is True for value in trial["checks"].values())
            or trial.get("original_before")!=trial.get("original_after")
            or trial.get("trial_writers_disabled") is not True):
        raise ValueError("Completed, bounded owned strategy measurement required")
    measured = source_identity()
    if measured != trial.get("implementation_identity"):
        raise ValueError("Measured implementation changed after the actual trial; requalify")
    for name, expected in trial["source_file_hashes"].items():
        path = ROOT/name
        if not path.resolve().is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError("Executed test/adapter source differs from the actual trial")
    scope = trial.get("trial_scope",{})
    if (scope.get("native_catalog_version")!=2 or scope.get("writer_paths")!=2
            or scope.get("admin_connection_reuse") is not True
            or scope.get("conformance_documents_are_not_admitted_plans") is not True
            or scope.get("max_prepared_transactions")!=0):
        raise ValueError("Only the actually tested native catalog/engine profile may be qualified")
    def connect():
        return psycopg.connect((args.control_directory/"runtime.dsn").read_text(),connect_timeout=5)
    registry = PostgresRegistrations(connect,tenant_id=identity["tenant_id"])
    contract = registry.current("service_contract",identity["application_id"]).value
    profiles = {"source":registry.current("environment_profile","aws-ec2-b0be270e62").value,
                "target":registry.current("environment_profile","local-rke2-b0be270e62").value}
    observations = {}
    for side in ("source","target"):
        entry = identity["environments"][side]
        dsn = make_conninfo(host=entry["database_host"],hostaddr="127.0.0.1",port=links["ports"][side+"_db"],
            user="postgres",password=(args.inputs/("client/"+side+"-admin.password")).read_text(),
            dbname=entry["database"],sslmode="verify-full",sslrootcert=str(args.inputs/"pki/ca.pem"),connect_timeout=5)
        with psycopg.connect(dsn) as conn:
            system,version,prepared = conn.execute("SELECT (SELECT system_identifier::text FROM pg_control_system()),"
                "current_setting('server_version'),current_setting('max_prepared_transactions')").fetchone()
        if (system!=trial[side+"_system_identifier"] or version.split()[0]!=dict(profiles[side].versions)["postgresql"]
                or int(prepared)!=0):
            raise ValueError("Current engine identity/version differs from the tested profile")
        observations[side] = {"system_identifier":system,"postgresql":version.split()[0],"verified_tls":True,
                              "max_prepared_transactions":int(prepared)}
    trust = PostgresVerifierRegistry(connect,tenant_id=identity["tenant_id"],application_id=identity["application_id"]).resolve(ISSUER,KEY_ID)
    if trust.revoked or not trust.valid_from<=now<trust.valid_until or until>trust.valid_until or VERIFIER not in trust.allowed_verifiers:
        raise ValueError("Approved verifier key is not currently valid for this trial")
    sys.path.insert(0,"/Users/Sooming_/dev/softbank_2026/personal-development/.local-deps/personal-keychain")
    from keyring.backends.macOS import Keyring

    private = Keyring().get_password("sova.personal.registry.b0be270e62","registry-producer-v3")
    if private is None:
        raise ValueError("Existing independently approved verifier key required; do not create a substitute")
    signer = Ed25519EvidenceSigner(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private)),issuer_id=ISSUER,key_id=KEY_ID)
    if signer.public_key_hex!=trust.public_key_hex:
        raise ValueError("Keychain private key differs from the approved public key")
    raw_digest = hashlib.sha256(raw).hexdigest()
    package = {"version":1,"run_id":identity["run_id"],"observed_at":now.isoformat(),"expires_at":until.isoformat(),
        "implementation_digest":measured["digest"],"raw_trial_sha256":raw_digest,"current_engines":observations,
        "registrations":[],"attestations":[],"business_plan_admitted":False,"continuity_qualified":False,
        "qualification_scope":"This measured 3-table/2-writer canary, PostgreSQL 17.12/arm64, prepared transactions disabled; not arbitrary apps"}
    for direction,strategy,source,target,key in (("forward","offline-copy","source","target","source_rows"),
                                               ("reverse",FAILBACK_STRATEGY,"target","source","target_rows_after_new_write")):
        operations = required_operations(strategy)
        seen = [step for step in trial["steps"] if step["direction"]==direction]
        if len(seen)!=len(operations) or set(step["action"] for step in seen)!={ACTIONS[operation] for operation in operations} or any(
            step.get("status")!="confirmed_applied" or step.get("fresh_instance_readback") is not True
            or step.get("repeated_apply_same_observed_state") is not True for step in seen):
            raise ValueError("Every claimed operation must have actually passed including repeated effects")
        rows = scope[key]
        if set(rows)!={"orders","work_items","idempotency_results"} or any(type(count) is not int or count<1 for count in rows.values()):
            raise ValueError("Actually measured per-table row counts required")
        manifest = CapabilityManifest("postgres-offline-copy","0.1.0",measured["digest"],strategy,"1.0.0",
            operations,(MigrationProfile.SINGLE_AUTHORITY,))
        qualification = StrategyQualification("personal-"+direction+"-"+raw_digest[:20],identity["tenant_id"],identity["application_id"],
            contract.fingerprint,manifest.fingerprint,profiles[source].fingerprint,profiles[target].fingerprint,
            MigrationProfile.SINGLE_AUTHORITY,operations,sum(rows.values()),now,until,
            "personal-strategy-verifier-v1",hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            trial["source_file_hashes"]["tests/qualification_personal_strategy.py"],raw_digest,EvidenceGrade.LIVE)
        for kind,value in (("capability_manifest",manifest),("strategy_qualification",qualification)):
            package["registrations"].append({"kind":kind,"subject":subject_of(value),"document":encode(value)})
        record = attestation_record(qualification,verifier_version=VERIFIER,observed_at=now,valid_until=until)
        if record.scope not in trust.allowed_scopes:
            raise ValueError("Qualification signature outside approved verifier scope")
        package["attestations"].append(signer.sign(record,issued_at=now).document)
    # The contract signature authenticates the exact registered requirements;
    # it does not claim those requirements have been satisfied by migration.
    record = attestation_record(contract,verifier_version=VERIFIER,observed_at=now,valid_until=until)
    if record.scope not in trust.allowed_scopes:
        raise ValueError("Contract signature outside approved verifier scope")
    package["attestations"].append(signer.sign(record,issued_at=now).document)
    args.output.write_text(canonical_json(package,limit=1<<20)+"\n")
    print(json.dumps({"directional_qualifications":2,"signatures":3,"measured_rows":{
        direction:sum(scope[key].values()) for direction,key in (("forward","source_rows"),("reverse","target_rows_after_new_write"))},
        "implementation_digest":measured["digest"],"business_plan_admitted":False,"continuity_qualified":False}))


if __name__=="__main__":
    main()
