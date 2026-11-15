"""Leaving GCP completely, measured: one contract removes every GCP dependency.

The GCP counterpart of the AWS exit. One service contract declares the
removal of its Cloud SQL database, its Cloud Storage bucket, its Secret
Manager secrets and its Identity Platform tenant; its compute runs on Cloud
Run. Each moves on premises (PostgreSQL, an S3-compatible store, Kubernetes
Secrets, Keycloak, a new application session) through its own adapter, the
data through the whole control-plane protocol with the identity obligations
the contract adds. Then every GCP source is really blocked (database closed
to connections, bucket grants removed by the fence, secret versions disabled,
the tenant's password sign-in off, Cloud Run scaled to zero) and every exit
drill is measured. The route step switches a real Cloud DNS name from the
Cloud Run front end to the on-premises address. Last, the platform leaves:
the customer revokes every credential the platform held and operates alone.
Run-scoped resources only; values and passwords never printed.
"""

import base64
from datetime import UTC, datetime, timedelta
import json
import secrets
import socket
import time
from urllib.parse import urlparse

from adapter_conformance import SEQUENCES, apply
from aws_fixtures import reset_password
from gcp_fixtures import SECRETS_API, idp_login, run_answers
from gcs_world import GcsWorld
from handoff_drive import run_handoff, without_transport_control
from keycloak_fixtures import CLIENT
import platform_exit_support as platform
import psycopg
from psycopg import sql
import pytest

from sova.adapters import platform_lockout as lockout
from sova.adapters.gcp_cloud_dns_route import CloudDnsRoute
from sova.adapters.gcp_cloud_run_compute import CloudRunServiceCompute
from sova.adapters.gcp_rest import GcpApiError
from sova.adapters.gcp_secret_manager import SecretManagerToKubernetes
from sova.adapters.keycloak_identity import KeycloakIdentityMigration
from sova.adapters.oidc_probes import OidcProbes
from sova.adapters.postgres_handoff_probes import artifact_digest
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.gcp_live, pytest.mark.postgres, pytest.mark.keycloak,
              pytest.mark.kubernetes, pytest.mark.s3]
ONPREM_ADDRESS = "203.0.113.20"  # documentation range: the route's control, not reachability
REMOVED = ["gcp-identity-platform", "source-db", "source-object-store", "source-secret-service"]
IDENTITY = {
    "principal_namespace": "organization-scoped-stable-id",
    "stable_subject_mapping_required": True,
    "allowed_external_idps": ["customer-owned-idp"],
    "remove_dependencies": ["gcp-identity-platform"],
    "allow_dual_issuer_during_transition": False,
    "dual_issuer_max_duration": None,
    "password_transition_policy": "reset",
    "mfa": {"required": False, "allowed_methods": [], "reenrollment_allowed": True,
            "user_communication_approved": True},
    "sessions": {"forced_reauthentication_allowed": True,
                 "refresh_token_portability_assumed": False},
    "authorization": {"deny_cross_tenant_access": True,
                      "source_target_permission_equivalence_required": True},
    "evidence": {"fresh_login_required": True, "fresh_token_refresh_required": True,
                 "denied_access_test_required": True,
                 "source_idp_blocked_cold_start_required": True},
}
PROPAGATION_SECONDS = 60  # the GCS fence's IAM window in tests (see test_gcp_storage_live)


def leave_gcp(data):
    without_transport_control(data)
    data["spec"]["exit"] = {"remove": list(REMOVED),
                            "allowed_remaining": ["customer-owned-idp"]}
    data["spec"]["identity"] = json.loads(json.dumps(IDENTITY))


def command(adapter_id, action, unit="unit-exit"):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "onprem-target", unit), "op-exit", action,
        adapter_id, action, f"op-exit:{action}", "e" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=30), "{}")


def run(adapter, action):
    cmd = command(adapter.adapter_id, action)
    adapter.validate(cmd)
    adapter.apply(cmd)
    return adapter.readback(cmd, datetime.now(UTC))


def test_leaving_gcp_completely_is_measured_dependency_by_dependency(
        postgres_database, cloudsql_business, gcs_stores, s3_stores, gcp_secrets, idp_tenant,
        keycloak_target, keycloak_servers, kind_secrets, cloud_run_app, dns_zone, gcp_rest):
    db_exec = platform.postgres_exec()
    if not db_exec or s3_stores["platform"] is None:
        pytest.skip("SOVA_TEST_POSTGRES_EXEC (or _CONTAINER) and SOVA_TEST_S3_PLATFORM are required")
    b, kc = cloudsql_business, keycloak_target
    target_probes = OidcProbes(client_id=CLIENT)
    compute = CloudRunServiceCompute("cloud-run-source", rest=gcp_rest,
                                     region=cloud_run_app["region"], service=cloud_run_app["name"])
    old = compute.endpoints()
    assert run_answers(cloud_run_app["url"], cloud_run_app["token"])
    front_end = socket.gethostbyname(urlparse(cloud_run_app["url"]).hostname)
    # The name clients use points at the source application's front end.
    record = "orders." + dns_zone["name"]
    route = CloudDnsRoute(dns_zone["rest"], zone=dns_zone["zone"], name=record, ttl=60,
                          sides={b["names"]["src"]: front_end, b["names"]["tgt"]: ONPREM_ADDRESS})
    route.set(b["names"]["src"])
    assert route.get() == b["names"]["src"]

    # -- identity first: users must sign in at the target before activation ----
    kc["server"].call("PUT", f"/{kc['realm']}", {
        "realm": kc["realm"], "enabled": True, "eventsEnabled": True,
        "adminEventsEnabled": True, "bruteForceProtected": True,
        "resetPasswordAllowed": True})
    other = "tenant-b-" + kc["realm"]
    keycloak_servers["target"].realm(other)
    keycloak_servers["target"].user(other, "mallory", "Mallory-pw-1!")
    identity = KeycloakIdentityMigration("identity-platform-to-keycloak",
                                         source=idp_tenant["directory"], target=kc["admin"],
                                         mfa_required=False)
    migrated_at = int(time.time() * 1000)
    assert run(identity, "migrate_identities").status == EffectStatus.APPLIED
    users = {"alice": "alice@example.test", "bob": "bob@example.test"}
    onprem_password = {name: f"{name.title()}-onprem-" + secrets.token_urlsafe(8) + "!1"
                       for name in users}
    for name, login in users.items():
        reset_password(kc, login, onprem_password[name])  # what each user does at the reset page
    source_tokens, error = idp_login(idp_tenant, users["alice"], idp_tenant["passwords"]["alice"])
    assert error is None, error

    # -- secrets and objects --------------------------------------------------
    secrets_adapter = SecretManagerToKubernetes(
        "secrets-handoff", rest=gcp_rest, controls=kind_secrets["controls"](),
        mappings={gcp_secrets["names"]["db"]: ("orders-db", "credentials"),
                  gcp_secrets["names"]["api"]: ("orders-api", "api-key")})
    assert run(secrets_adapter, "copy_secrets").status == EffectStatus.APPLIED
    objects = GcsWorld(gcs_stores, s3_stores, propagation_seconds=PROPAGATION_SECONDS)
    try:
        for action in SEQUENCES["offline-copy"]:
            apply(objects.adapter(), action)
        assert objects.content("source") == objects.content("target")
        _leave(b, kc, target_probes, idp_tenant, users, onprem_password, migrated_at,
               identity, source_tokens, route, record, compute, old, cloud_run_app,
               objects, gcp_secrets, gcp_rest, kind_secrets, keycloak_servers, s3_stores,
               postgres_database, db_exec)
    finally:
        objects.cleanup()


def _leave(b, kc, target_probes, idp_tenant, users, onprem_password, migrated_at, identity,
           source_tokens, route, record, compute, old, app, objects, gcp_secrets, gcp_rest,
           kind_secrets, keycloak_servers, s3_stores, postgres_database, db_exec):
    # -- the data, through the protocol, with the identity obligations ---------
    def readback_of(adapter, action):
        observation = adapter.readback(command(adapter.adapter_id, action), datetime.now(UTC))
        return observation.status == EffectStatus.APPLIED, observation.evidence

    def no_credential_copied(_):
        found = []
        for login in users.values():
            account = kc["server"].find(kc["realm"], login)
            credentials = kc["server"].call(
                "GET", f"/{kc['realm']}/users/{account['id']}/credentials")
            found += [c["type"] for c in credentials if c.get("createdDate", 0) < migrated_at]
        return not found, {"probe": "no_credential_copied", "pre_migration_credentials": found}

    def permission_equivalence(_):
        source_claims = OidcProbes(client_id=idp_tenant["project"]).verify(
            idp_tenant["directory"].issuer, source_tokens["idToken"])
        tokens, error = target_probes.login(kc["issuer"], users["alice"], onprem_password["alice"])
        if error:
            return False, {"probe": "permission_equivalence", "target": error}
        claims = target_probes.verify(kc["issuer"], tokens["id_token"])
        source_groups = sorted(source_claims.get("groups", []))
        return source_groups == sorted(claims.get("groups", [])), {
            "probe": "permission_equivalence", "source": source_groups,
            "target": sorted(claims.get("groups", []))}

    def audit_lock_recovery(_):
        realm = kc["server"].call("GET", f"/{kc['realm']}")
        paths = {k: realm.get(k) is True for k in ("eventsEnabled", "bruteForceProtected",
                                                   "resetPasswordAllowed")}
        return all(paths.values()), {"probe": "audit_lock_recovery_paths", **paths}

    def login_slo(obligation):
        started = time.monotonic()
        passed, observation = target_probes.fresh_login(kc["issuer"], users["bob"],
                                                        onprem_password["bob"])
        spent = (time.monotonic() - started) * 1000
        budget = contract_availability["max_client_completion_wait_ms"]
        return passed and spent <= budget, {"probe": "login_slo", "spent_ms": round(spent),
                                            "budget_ms": budget, "login": observation}

    contract_availability = {}

    def adjust(data):
        leave_gcp(data)
        contract_availability.update(data["spec"]["availability"])

    measures = {
        "IAM-07.no_secret_or_pii_leak": no_credential_copied,
        "IAM-04.cross_tenant_denial": lambda _: target_probes.cross_tenant_denied(
            kc["issuer"], "mallory", "Mallory-pw-1!"),
        "IAM-02.fresh_login_and_validation": lambda _: target_probes.fresh_login(
            kc["issuer"], users["alice"], onprem_password["alice"]),
        "IAM-02.fresh_token_refresh": lambda _: target_probes.fresh_refresh(
            kc["issuer"], users["alice"], onprem_password["alice"]),
        "IAM-04.permission_equivalence": permission_equivalence,
        "IAM-01.subject_mapping_unique": lambda _: readback_of(identity, "migrate_identities"),
        "IAM-06.account_state_parity": lambda _: readback_of(identity, "migrate_identities"),
        "IAM-09.audit_lock_recovery_paths": audit_lock_recovery,
        "IAM-12.login_slo": login_slo,
    }
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), b,
                    adjust=adjust, measures=measures, route=route)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["record"].held_from, x["observed"].get("uncertain"),
        x["observed"].get("failed"))
    # The route step moved the DNS name: every authoritative server agrees.
    observed = route.observe()
    assert observed["route"] == b["names"]["tgt"] and observed["answer"] == [ONPREM_ADDRESS]

    # -- what the platform still holds on premises -----------------------------
    tgt, local = b["names"]["tgt"], b["servers"][b["names"]["tgt"]]
    s3_root = objects.root["target"]
    granted = platform.grant(admin_dsn=local, target_db=tgt,
                             keycloak=keycloak_servers["target"], s3_root=s3_root,
                             bucket=objects.buckets["target"],
                             platform_principal=s3_stores["platform_principal"])
    held = platform.lockouts(
        granted, kubernetes_request=lambda: kind_secrets["controls"]().secret("orders-db"),
        admin_dsn=local, target_db=tgt, keycloak=keycloak_servers["target"],
        platform_s3=s3_stores["platform"]("target"), bucket=objects.buckets["target"])
    assert {name: probe()[0] for name, probe in held.items()} == dict.fromkeys(held, False)

    # -- block every GCP source for real ---------------------------------------
    assert run(compute, "stop_source_compute").status == EffectStatus.APPLIED
    with psycopg.connect(b["servers"][b["names"]["src"]], autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
            sql.Identifier(b["names"]["src"])))
    for name in gcp_secrets["names"].values():
        versions = gcp_rest.get(f"{SECRETS_API}/projects/{gcp_rest.project}/secrets/{name}"
                                "/versions").get("versions", [])
        for version in versions:
            if version.get("state") == "ENABLED":
                gcp_rest.post(f"{SECRETS_API}/{version['name']}:disable")
    idp_tenant["rest"].patch(idp_tenant["tenant_url"], {"allowPasswordSignup": False},
                             params={"updateMask": "allowPasswordSignup"})

    # -- measure each exit drill -----------------------------------------------
    def new_session_does_business():
        return x["probes"].cold_start_without_source()

    def compute_blocked():
        stopped, run_state = compute.observe()
        answered = any(run_answers(url, app["token"]) for url in old)
        serves, target = new_session_does_business()
        return stopped and not answered and serves, {
            "probe": "source_compute_blocked", "cloud_run": run_state,
            "old_endpoint": "answered" if answered else "unreachable", "target": target}

    def object_store_blocked():
        source_refused = not objects.can_write("source", "app")
        reader = objects._as("source", "app")
        try:
            reader.get_object(Bucket=objects.buckets["source"], Key="orders/0001.json")
            source_refused = False
        except Exception as refused:  # noqa: BLE001 - only an access denial counts
            source_refused = source_refused and "AccessDenied" in str(refused)
        target = objects.s["as"]("target", "app")
        reads = target.get_object(Bucket=objects.buckets["target"],
                                  Key="orders/0001.json")["ContentLength"] > 0
        writes = objects.can_write("target", "app")
        return source_refused and reads and writes, {
            "probe": "object_store_blocked", "source_refused": source_refused,
            "target_reads": reads, "target_writes": writes}

    def secret_service_blocked():
        refused = {}
        for key, name in gcp_secrets["names"].items():
            try:
                gcp_rest.get(f"{SECRETS_API}/projects/{gcp_rest.project}/secrets/{name}"
                             "/versions/latest:access")
                refused[key] = False
            except GcpApiError as error:
                refused[key] = error.reason in ("FAILED_PRECONDITION", "NOT_FOUND",
                                                "PERMISSION_DENIED")
        controls = kind_secrets["controls"]()
        delivered = {
            "db": base64.b64decode(controls.secret("orders-db")["data"]["credentials"])
            == gcp_secrets["values"]["db"],
            "api": base64.b64decode(controls.secret("orders-api")["data"]["api-key"])
            == gcp_secrets["values"]["api"]}
        return all(refused.values()) and all(delivered.values()), {
            "probe": "secret_service_blocked", "source_refused": refused,
            "target_delivers": delivered}

    def identity_provider_blocked():
        blocked, error = idp_login(idp_tenant, users["bob"], idp_tenant["passwords"]["bob"])
        passed, observation = target_probes.fresh_login(kc["issuer"], users["bob"],
                                                        onprem_password["bob"])
        return blocked is None and passed, {"probe": "source_idp_blocked_cold_start",
                                            "source": error, "target": observation}

    def old_issuer_rejected():
        try:
            target_probes.verify(kc["issuer"], source_tokens["idToken"])
            return False, {"probe": "old_issuer_rejected", "outcome": "accepted"}
        except ContractError as refused:
            return True, {"probe": "old_issuer_rejected", "outcome": str(refused)}

    def fresh_restart():
        serves, data = new_session_does_business()
        login, token = target_probes.fresh_login(kc["issuer"], users["alice"],
                                                 onprem_password["alice"])
        secret, delivered = secret_service_blocked()
        return serves and login and secret, {"probe": "cold_start_fresh_credentials",
                                              "data": data, "token": token,
                                              "secrets": delivered["target_delivers"]}

    measured = {
        "exit.compute.source_compute_blocked": compute_blocked,
        "exit.removed.source-db.blocked_cold_start": new_session_does_business,
        "exit.removed.source-object-store.blocked_cold_start": object_store_blocked,
        "exit.removed.source-secret-service.blocked_cold_start": secret_service_blocked,
        "exit.removed.gcp-identity-platform.blocked_cold_start": identity_provider_blocked,
        "IAM-08.source_idp_blocked_cold_start": identity_provider_blocked,
        "IAM-10.old_issuer_rejected_after_end": old_issuer_rejected,
        "exit.restart.cold_start_fresh_credentials": fresh_restart,
        "exit.operations.batch_backup_restore_audit_deploy":
            lambda: x["probes"].restore_drill(b["names"]["scratch"]),
    }
    results, observations = {}, {}
    for o in x["readiness"].obligations(x["revision"], x["plan"]):
        assert o.obligation_id in measured or o.obligation_id.startswith("exit.platform."), \
            o.obligation_id
        if o.obligation_id in measured:
            passed, observations[o.obligation_id] = measured[o.obligation_id]()
            results[o.obligation_id] = passed
            x["prove"](o.obligation_id, VerificationResult.PASS if passed else
                       VerificationResult.FAIL, obligation=o,
                       artifact_digest=artifact_digest(observations[o.obligation_id]))
    assert all(results.values()), {k: observations[k] for k, v in results.items() if not v}

    # -- last, the platform leaves: the customer revokes it and runs alone -----
    platform.revoke(granted, context=kind_secrets["context"],
                    namespace=kind_secrets["namespace"], admin_dsn=local,
                    keycloak=keycloak_servers["target"], s3_root=s3_root,
                    bucket=objects.buckets["target"],
                    platform_principal=s3_stores["platform_principal"])
    operations = platform.customer_operations(
        context=kind_secrets["context"], namespace=kind_secrets["namespace"],
        secret="orders-db", key="credentials", db_exec=db_exec, admin_dsn=local,
        target_db=tgt, app_connect=lambda: b["connect_as"](tgt, b["app"]),
        keycloak=keycloak_servers["target"], realm=kc["realm"], issuer=kc["issuer"],
        user=users["alice"], password="Alice-customer-" + secrets.token_urlsafe(6) + "!3")
    passed, observation = lockout.platform_exit(held, operations)
    assert passed, observation
    drill = next(o for o in x["readiness"].obligations(x["revision"], x["plan"])
                 if o.obligation_id == "exit.platform.customer_operates_after_revocation")
    x["prove"](drill.obligation_id, VerificationResult.PASS, obligation=drill,
               artifact_digest=artifact_digest(observation))
    report = x["readiness"].assess(x["revision"], x["plan"])
    levels = {k: v["status"] for k, v in report["levels"].items()}
    assert levels == {**levels, "COMPUTE_INDEPENDENT": "PASS",
                      "DATA_IDENTITY_INDEPENDENT": "PASS", "RESTART_VERIFIED": "PASS",
                      "OPERATIONS_VERIFIED": "PASS", "PROVIDER_EXIT_VERIFIED": "PASS",
                      "PLATFORM_EXIT_VERIFIED": "PASS"}, levels
    print("exit levels:", json.dumps(levels, sort_keys=True))
