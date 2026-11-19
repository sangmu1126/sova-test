"""Map projected live facts into the existing dependency registry contract.

Observed SQL/container identities become nodes, not deployment guesses. The
current collector does not establish complete automation/privilege closure;
the snapshot explicitly retains that uncertainty and cannot authorize a move.
"""

from datetime import datetime, timedelta
import hashlib
import json

from sova.domain.contracts import ContractError, ResourceScope, utc
from sova.domain.migration_units import DependencySnapshot
from sova.domain.system_graph import Portability, RelationKind, ResourceKind, SystemGraph, SystemRelation, SystemResource


def snapshot_from_facts(facts, contract, *, now):
    observed=utc(datetime.fromisoformat(facts["observed_at"]))
    now=utc(now)
    if not observed<=now<observed+timedelta(minutes=10):
        raise ContractError("Recent live dependency facts required")
    if (facts["run_id"]!="b0be270e62" or facts["aws"]["account"]!="265233844540"
            or facts["aws"]["instance_id"]!="i-04946f0e0c0548c3b"
            or facts["target"]["namespace_uid"]!="565b91c4-4f6e-4d06-94c6-9f8803afa6c9"
            or facts.get("business_data_changed") is not False):
        raise ContractError("Exact owned measured environment required")
    app=facts["source"]["application_projection"]
    if (app["tenant"],app["application"],app["run"],app["database"],app["role"])!=(
            contract.tenant_id,contract.service_id,"b0be270e62","source_orders","orders_app"):
        raise ContractError("Measured app does not belong to this contract")
    containers=facts["source"]["containers"]
    if len(containers)!=2 or {x["name"].lstrip("/") for x in containers}!={"orders-app","orders-db"}:
        raise ContractError("Exact source container inventory required")
    by_name={x["name"].lstrip("/"):x for x in containers}
    if any(x["run_id"]!="b0be270e62" or x["running"] is not True or not x["id"] for x in containers):
        raise ContractError("Running owned container identities required")
    if not by_name["orders-app"]["registered_image"].endswith("@"+app["image_digest"]):
        raise ContractError("Actual container artifact and application configuration disagree")
    db=facts["databases"]["source"]
    if db["system_identifier"]!="7709720208587968539" or db["database"]!="source_orders" or db["tls"][0] is not True:
        raise ContractError("Exact TLS database identity required")
    roles=[r for r in db["roles"] if r["name"]=="orders_batch"]
    if len(roles)!=1 or not roles[0]["oid"]:
        raise ContractError("Measured batch authority required")
    protected=contract.document["spec"]["data"]["protected_sets"]
    if set(protected)!={"orders","work_items","idempotency_results"}:
        raise ContractError("This projection supports only the measured three-table canary")
    table_names={r[0] for r in db["tables"]}
    if not set(protected)<=table_names:
        raise ContractError("Protected data is missing from actual SQL inventory")
    raw_digest=hashlib.sha256(json.dumps(facts,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    environment="aws-ec2-b0be270e62"
    resources=[]
    for node,name,kind,backend,revision in (
        ("source-api","orders-app",ResourceKind.SERVICE,"containerd",by_name["orders-app"]["registered_image"]),
        ("source-database","source_orders",ResourceKind.DATABASE,"postgresql",db["version"]),
        ("source-batch-authority","orders_batch SQL authority",ResourceKind.WORKER,"postgresql-role",roles[0]["oid"])):
        resources.append(SystemResource(node,name,ResourceScope(contract.tenant_id,contract.service_id,environment,node),
            kind,backend,"aws-ap-northeast-2",revision,Portability.UNKNOWN,("partial-live-inventory",)))
    uids=(("source-api",by_name["orders-app"]["id"]),
        ("source-database","pg-"+db["system_identifier"]+"-db-"+db["database_oid"]),
        ("source-batch-authority","pg-"+db["system_identifier"]+"-role-"+roles[0]["oid"]))
    valid_until=min(observed+timedelta(minutes=10),utc(datetime.fromisoformat(app["expires_at"])))
    if now>=valid_until:
        raise ContractError("Actual application authorization expired")
    graph=SystemGraph(contract.tenant_id,contract.service_id,1,"registration",observed,
        valid_until,tuple(resources),(
            SystemRelation("source-api","source-database",RelationKind.WRITES,True),
            SystemRelation("source-batch-authority","source-database",RelationKind.WRITES,True)))
    return DependencySnapshot("measured-dependencies-"+raw_digest[:20],contract.fingerprint,graph,uids,
        tuple((name,"source-database") for name in protected),
        ("source-app-tls-database-configuration","all-sql-role-catalog","sql-set-role-reachability",
         "security-definer-call-inventory","source-container-identity"),
        ("complete-controller-inventory","complete-writer-path-coverage","external-change-feed-coverage",
         "destination-placement-observer","audit-delivery-path"),False,False)


def main():
    import argparse
    from datetime import UTC, datetime
    import os
    from pathlib import Path
    import sys

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import psycopg

    from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
    from sova.adapters.postgres_registrations import PostgresRegistrations
    from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
    from sova.adapters.registration.registry_codec import encode
    from sova.application.registry_authentication import postgres_registry_authenticator
    from sova.domain.contracts import canonical_json
    from sova.domain.registry_attestation import attestation_record
    from sova.service.directional_catalog import source_identity

    p=argparse.ArgumentParser(description=__doc__)
    for name in ("inputs","facts","control-directory","output"):
        p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args()
    os.umask(0o077)
    inputs=json.loads((a.inputs/"identity.json").read_text())
    raw=a.facts.read_bytes()
    if len(raw)>4<<20 or a.output.exists():
        raise ValueError("New output and bounded measured facts required")
    now=datetime.now(UTC)
    if inputs["run_id"]!="b0be270e62" or now>=datetime.fromisoformat(inputs["expires_at"]):
        raise ValueError("Unexpired owned trial required")
    def connect():
        return psycopg.connect((a.control_directory/"runtime.dsn").read_text(),connect_timeout=5)
    registry=PostgresRegistrations(connect,tenant_id=inputs["tenant_id"])
    registered=registry.current("service_contract",inputs["application_id"])
    if registered is None:
        raise ValueError("Existing authenticated service contract required")
    contract=registered.value
    authenticate=postgres_registry_authenticator(connect,tenant_id=inputs["tenant_id"],
        application_id=inputs["application_id"],verifier_version="registry-attestor-1",clock=lambda:datetime.now(UTC))
    if authenticate(contract) is not True:
        raise ValueError("Current contract authentication failed")
    snapshot=snapshot_from_facts(json.loads(raw),contract,now=now)
    trust=PostgresVerifierRegistry(connect,tenant_id=inputs["tenant_id"],application_id=inputs["application_id"]).resolve(
        "personal-registry-verifier-b0be270e62","registry-v3")
    end=min(snapshot.graph.valid_until,trust.valid_until,datetime.fromisoformat(inputs["expires_at"]))
    record=attestation_record(snapshot,verifier_version="registry-attestor-1",observed_at=now,valid_until=end)
    if trust.revoked or not trust.valid_from<=now<end or record.scope not in trust.allowed_scopes or "registry-attestor-1" not in trust.allowed_verifiers:
        raise ValueError("Current independently registered verifier scope required")
    sys.path.insert(0,"/Users/Sooming_/dev/softbank_2026/personal-development/.local-deps/personal-keychain")
    from keyring.backends.macOS import Keyring
    private=Keyring().get_password("sova.personal.registry.b0be270e62","registry-producer-v3")
    if private is None:
        raise ValueError("Never create a substitute signing identity")
    signer=Ed25519EvidenceSigner(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private)),
        issuer_id="personal-registry-verifier-b0be270e62",key_id="registry-v3")
    if signer.public_key_hex!=trust.public_key_hex:
        raise ValueError("Keychain key differs from registered verifier")
    if registry.current("dependency_snapshot",snapshot.snapshot_id) is not None:
        raise ValueError("Never issue another attestation for the same snapshot")
    package={"version":1,"run_id":inputs["run_id"],"observed_at":now.isoformat(),"expires_at":end.isoformat(),
        "implementation_digest":source_identity()["digest"],"raw_facts_sha256":hashlib.sha256(raw).hexdigest(),
        "registrations":[{"kind":"dependency_snapshot","subject":snapshot.snapshot_id,"document":encode(snapshot),"expected_revision":0}],
        "attestations":[signer.sign(record,issued_at=now).document],"business_plan_admitted":False,
        "continuity_qualified":False,"preissued_measured_qualifications":0,"partial_inventory":True}
    with a.output.open("x") as f:
        f.write(canonical_json(package,limit=1<<20)+"\n")
    print(json.dumps({"measured_nodes":len(snapshot.graph.resources),"declared_unknown_paths":len(snapshot.unresolved_paths),
        "writer_inventory_complete":False,"control_inventory_complete":False,"migration_authorized":False}))


if __name__=="__main__":
    main()
