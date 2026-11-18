"""Local MQ-03 wiring, not a deployment or a live qualification issuer.

Only registry/evidence trust and change-feed watermarks are fixture bootstrap.
Registrations, plans, approvals, admission and failback requests use real TCP
HTTP. The unchanged HandoffWorker executes the admitted database effects.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from handoff_drive import local_budgets
from postgres_business import PROBE_INSERT, SCHEMA, TABLES, Route
import httpx
import psycopg
from qualification_orders import serve
from qualification_diagnostics import ExceptionDiagnostics
from test_service_api import AUDIENCE, ISSUER, Issuer
from test_service_control_plane import feeds_complete, request_of

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.registration import registry_codec as codecs
from sova.application.service_contracts import example_service_contract
from sova.domain.plan_compiler import FAILBACK_STRATEGY
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service.api import create_app
from sova.service.auth import BearerAuthenticator
from sova.service.control_plane import ControlPlane, Settings
from sova.service.postgres_execution import postgres_factory
from sova.service.worker import AdapterCatalog, HandoffWorker


class ServiceJourney:
    def __init__(self, postgres_database, business, directory):
        dsn, role = postgres_database
        self.connect = lambda: psycopg.connect(dsn, user=role)
        self.clock = lambda: datetime.now(UTC)
        self.tenant = "tenant-" + uuid4().hex
        data = example_service_contract().document
        data["metadata"]["tenant_id"] = self.tenant
        local_budgets(data)  # generous test budgets; never a production SLO
        self.world = build_world(self.clock() - timedelta(seconds=1),
                                 contract=ServiceContract.from_document(data))
        # Directional test profiles must at least name the actual engine
        # versions, even though inventory/qualification remain synthetic.
        self.database_versions = {}
        for side, database in (("source", "src"), ("target", "tgt")):
            with business["connect"](business["names"][database]) as conn:
                version = conn.execute("SHOW server_version").fetchone()[0].split()[0]
            self.database_versions[self.world[side].profile_id] = version
        self.bind_database_versions(self.world)
        self.app = self.world["contract"].service_id
        witness = FileAuthorityWitness(directory / "authority.log", tenant_id=self.tenant)
        self.settings = Settings("registry-attestor-1", "handoff-verifier-1",
                                 "handoff-producer", lambda tenant: witness)
        self.plane = ControlPlane(self.connect, settings=self.settings, clock=self.clock)
        self.registry = PostgresVerifierRegistry(self.connect, tenant_id=self.tenant,
                                                 application_id=self.app)
        self.publish_trust(self.world["trust"], version=1)
        self.signer = Ed25519EvidenceSigner(Ed25519PrivateKey.generate(),
                                            issuer_id="handoff-producer", key_id="k1")
        self.scopes = set()
        self.trust_version = 0
        self.issuer = Issuer()  # generated fixture JWKS, not a real external IdP
        self.authenticate = BearerAuthenticator(issuer=ISSUER, audience=AUDIENCE,
                                                fetch_jwks=self.issuer.jwks)
        self.business = business
        self.route = Route(business["names"]["src"])
        self.base = f"/v1/tenants/{self.tenant}"
        self.app_base = f"{self.base}/applications/{self.app}"
        self.trace = []
        self.client = None

    def bind_database_versions(self, world):
        for side in ("source", "target"):
            p = world[side]
            world[side] = replace(p, versions=tuple(
                (engine, self.database_versions[p.profile_id] if engine == "postgresql" else version)
                for engine, version in p.versions))
        world["qualification"] = replace(
            world["qualification"], source_profile_digest=world["source"].fingerprint,
            target_profile_digest=world["target"].fingerprint)

    def publish_trust(self, trust, *, version):
        at = self.clock() - timedelta(seconds=1)
        self.registry.publish(VerifierTrustChange(
            self.tenant, self.app, version, trust, "fixture-security-requester",
            "fixture-security-approver", "Local test producer bootstrap", at,
            at + timedelta(minutes=1)))

    def call(self, method, path, *, role="auditor", body=None, headers=None):
        response = self.client.request(method, path, json=body, headers=(
            self.issuer.token(self.tenant, role) if headers is None else headers))
        self.trace.append({"method": method, "path": path, "status": response.status_code,
                           "request_id": response.headers.get("x-request-id")})
        return response

    def register(self, values, *, attest=()):
        for value in values:
            kind, subject = codecs.kind_of(value), codecs.subject_of(value)
            response = self.call("PUT", f"{self.base}/registrations/{kind}/{subject}",
                                 role="registrar", body={"document": codecs.encode(value),
                                                         "expected_revision": 0})
            assert response.status_code == 200, response.text
        for subject in attest:
            response = self.call("POST", f"{self.base}/attestations", role="attestor",
                                 body=self.world["sign"](subject).document)
            assert response.status_code == 201, response.text

    def register_forward(self):
        w = self.world
        self.register((w["contract"], w["snapshot"], w["source"], w["target"], w["manifest"],
                       w["qualification"]), attest=(w["contract"], w["snapshot"], w["qualification"]))
        response = self.call("POST", f"{self.base}/plans", role="planner",
                             body=request_of(w).document)
        assert response.status_code == 201, response.text
        return response.json()

    def register_reverse(self):
        w = build_world(self.clock() - timedelta(seconds=1), contract=self.world["contract"],
                        strategy=FAILBACK_STRATEGY, reverse=True,
                        environment="synthetic-target", uid_prefix="target-uid-")
        self.bind_database_versions(w)
        # New subject IDs preserve the forward registration heads. The original
        # trusted registry signer attests them; build_world's new key is unused.
        w["snapshot"] = replace(w["snapshot"], snapshot_id=w["snapshot"].snapshot_id + "-return")
        w["qualification"] = replace(w["qualification"], record_id="test-record-return")
        self.register((w["snapshot"], w["manifest"], w["qualification"]),
                      attest=(w["snapshot"], w["qualification"]))
        return w

    def approve(self, compiled, approval):
        plan_path = f"{self.app_base}/plans/{compiled['plan_id']}"
        response = self.call("POST", plan_path + "/approvals", role="approver",
                             body={"approval_id": approval, "valid_minutes": 30})
        assert response.status_code == 201, response.text
        plan = PostgresLifecycleRepository(self.connect, tenant_id=self.tenant).get_plan(
            self.app, compiled["plan_id"])
        self.scopes.update(o.scope for o in plan.obligations)
        at = self.clock() - timedelta(seconds=1)
        self.trust_version += 1
        self.publish_trust(VerifierTrust(
            self.signer.issuer_id, self.signer.key_id, self.signer.public_key_hex,
            tuple(sorted(self.scopes, key=lambda s: (s.environment_id, s.resource_id))),
            (self.settings.evidence_verifier,), at - timedelta(minutes=1), at + timedelta(hours=1)),
            version=self.trust_version)
        # Synthetic complete-feed fixture, not a real change collector.
        feeds_complete({"connect": self.connect, "tenant": self.tenant}, compiled)
        return plan

    def admit(self, approval):
        response = self.call("POST", self.app_base + "/admissions", role="operator",
                             body={"approval_id": approval})
        assert response.status_code == 201, response.text
        return response.json()

    def approve_and_admit(self, compiled, approval):
        self.approve(compiled, approval)
        return self.admit(approval)

    def worker(self, *, forward_factory=None):
        b, names = self.business, self.business["names"]
        def resolve(reverse):
            def for_service(tenant, application):
                assert (tenant, application) == (self.tenant, self.app)
                return {"connect": b["connect"], "connect_as": b["connect_as"],
                        "source_db": names["tgt" if reverse else "src"],
                        "target_db": names["src" if reverse else "tgt"],
                        "scratch_db": names["scratch"], "other_db": names["other"],
                        "app_role": b["app"], "writer_roles": (b["app"], b["batch"]),
                        "tables": TABLES, "schema_sql": SCHEMA, "probe_insert": PROBE_INSERT,
                        "route": self.route}
            return for_service
        forward_id = self.world["manifest"].adapter_id
        forward = postgres_factory(forward_id, resolve(False))
        if forward_factory:
            forward = forward_factory(forward)
        reverse_id = "pg-" + FAILBACK_STRATEGY
        catalog = AdapterCatalog({forward_id: forward,
                                  reverse_id: postgres_factory(reverse_id, resolve(True))})
        return HandoffWorker(self.connect, settings=self.settings, catalog=catalog,
                             signer=self.signer, clock=self.clock, owner_id="mq03-worker")

    def status(self, unit):
        response = self.call("GET", f"{self.app_base}/units/{unit}")
        assert response.status_code == 200, response.text
        document = response.json()
        self.trace[-1]["authority_state"] = document.get("state")
        self.trace[-1]["next_operation"] = document.get("next_operation")
        self.trace[-1]["reason"] = document.get("reason")
        return document

    def record(self, name, **data):
        directory = os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS")
        if directory:
            with (Path(directory) / (name + ".json")).open("x") as file:
                json.dump({"case": name, "http_calls": self.trace,
                           "environment": "local service HTTP, existing worker, one PostgreSQL server",
                           "operational_qualification": False, **data}, file, indent=2, sort_keys=True)
                file.write("\n")


@contextmanager
def service_journey(postgres_database, business, directory):
    journey = ServiceJourney(postgres_database, business, directory)
    app = create_app(journey.plane, journey.authenticate)
    artifacts = os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS")
    if artifacts:
        app = ExceptionDiagnostics(app, Path(artifacts) / "service-exceptions.jsonl")
    with serve(app) as origin:
        with httpx.Client(base_url=origin, timeout=15, trust_env=False, follow_redirects=False) as client:
            journey.client = client
            yield journey
