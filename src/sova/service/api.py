"""HTTP API of the control plane (FastAPI).

Every route under ``/v1`` needs a bearer token (``sova.service.auth``); the
tenant in the path must be the token's tenant, and the role checks happen in
``ControlPlane``. Errors carry a short reason and never a stack, a query or a
provider message:

    401 no or invalid token      403 wrong tenant or role
    404 nothing registered       409 revision moved (compare-and-set)
    422 refused by a contract rule

``/healthz`` answers while the process runs; ``/readyz`` only when the database
answers and its schema is the one this code expects.
"""

from datetime import timedelta
import logging
import re
import uuid

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from sova.adapters.postgres_registrations import RegistrationConflict
from sova.adapters.registration import registry_codec as codecs
from sova.domain.contracts import ContractError
from sova.ports.plans import RecordNotFound, RepositoryConflict
from sova.service.auth import AuthenticationUnavailable, Unauthenticated
from sova.service.control_plane import Forbidden, NotFound, PlanRequest
from sova.service.http_boundary import RequestBodyBoundary

log = logging.getLogger("sova.api")
MAX_BODY = 1_048_576


def create_app(plane, authenticate, *, ready=lambda: True, operational_reads=None):
    app = FastAPI(title="Sova control plane", version="1", docs_url=None, redoc_url=None)
    app.add_middleware(RequestBodyBoundary,limit=MAX_BODY)

    @app.middleware("http")
    async def bounded(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}",supplied) else uuid.uuid4().hex
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            return JSONResponse({"error": "request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    @app.exception_handler(Unauthenticated)
    async def unauthenticated(request, error):
        return JSONResponse({"error": str(error)}, status_code=401,
                            headers={"www-authenticate": "Bearer"})

    @app.exception_handler(Forbidden)
    async def forbidden(request, error):
        return JSONResponse({"error": str(error)}, status_code=403)

    @app.exception_handler(AuthenticationUnavailable)
    async def authentication_unavailable(request, error):
        return JSONResponse({"error": "identity provider unavailable"}, status_code=503)

    @app.exception_handler(RegistrationConflict)
    async def conflict(request, error):
        return JSONResponse({"error": "revision moved",
                             "current_revision": error.current_revision}, status_code=409)

    @app.exception_handler(NotFound)
    async def not_found(request, error):
        return JSONResponse({"error": str(error)[:300]}, status_code=404)

    @app.exception_handler(RecordNotFound)
    async def record_not_found(request, error):
        return JSONResponse({"error": "record not found"}, status_code=404)

    @app.exception_handler(RepositoryConflict)
    async def revision_conflict(request, error):
        return JSONResponse({"error": "stored revision moved"}, status_code=409)

    @app.exception_handler(ContractError)
    async def refused(request, error):
        return JSONResponse({"error": str(error)[:300]}, status_code=422)

    def principal(tenant: str, authorization: str = Header(default=None)):
        who = authenticate(authorization)
        if who.tenant_id != tenant:
            raise Forbidden("The token belongs to another tenant")
        return who

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            if ready():
                return {"status": "ready"}
        except Exception:  # noqa: BLE001 - readiness reports, never raises
            log.exception("readiness check failed")
        return JSONResponse({"status": "not ready"}, status_code=503)

    @app.put("/v1/tenants/{tenant}/registrations/{kind}/{subject}")
    def register(kind: str, subject: str, body: dict = Body(...), who=Depends(principal)):
        if kind not in codecs.KINDS:
            raise ContractError("Unknown registry kind")
        if set(body) != {"document", "expected_revision"}:
            raise ContractError("Body is {document, expected_revision}")
        saved = plane.register(who, kind, subject, body["document"],
                               expected_revision=body["expected_revision"])
        return {"kind": kind, "subject_id": subject, "revision": saved.revision,
                "digest": saved.digest}

    @app.get("/v1/tenants/{tenant}/identity")
    def identity(who=Depends(principal)):
        return plane.identity(who)

    @app.get("/v1/tenants/{tenant}/registrations/{kind}")
    def registry_catalog(kind: str, after: str="", who=Depends(principal)):
        return plane.registry_catalog(who,kind,after)

    @app.get("/v1/tenants/{tenant}/applications/{application}/operational-observations")
    def operational_observations(application: str, who=Depends(principal)):
        return plane.operational_observations(who, application, operational_reads)

    @app.get("/v1/tenants/{tenant}/applications/{application}/change-history")
    def change_history(application: str, after: str="", who=Depends(principal)):
        return plane.change_history(who, application, after)

    @app.get("/v1/tenants/{tenant}/applications/{application}/plans")
    def plan_catalog(application: str, after: str="", who=Depends(principal)):
        return plane.plan_catalog(who,application,after)

    @app.get("/v1/tenants/{tenant}/applications/{application}/plans/{plan}")
    def plan_detail(application: str, plan: str, who=Depends(principal)):
        return plane.plan_detail(who,application,plan)

    @app.get("/v1/tenants/{tenant}/registrations/{kind}/{subject}")
    def registration(kind: str, subject: str, who=Depends(principal)):
        found = plane.registration(who, kind, subject)
        if found is None:
            return JSONResponse({"error": "nothing registered"}, status_code=404)
        return {"kind": kind, "subject_id": subject, "revision": found.revision,
                "digest": found.digest, "document": found.document,
                "registered_by": found.registered_by,
                "registered_at": found.registered_at.isoformat()}

    @app.put("/v1/tenants/{tenant}/applications/{application}/verifier-requests/{request_id}")
    def request_verifier(application: str, request_id: str, body: dict = Body(...), who=Depends(principal)):
        return plane.request_verifier_trust(who, application, request_id, body)

    @app.get("/v1/tenants/{tenant}/applications/{application}/verifier-requests/{request_id}")
    def verifier_request(application: str, request_id: str, who=Depends(principal)):
        return plane.verifier_trust_request(who, application, request_id)

    @app.post("/v1/tenants/{tenant}/applications/{application}/verifier-requests/{request_id}/approvals")
    def approve_verifier(application: str, request_id: str, body: dict = Body(...), who=Depends(principal)):
        return plane.approve_verifier_trust(who, application, request_id, body)

    @app.post("/v1/tenants/{tenant}/attestations", status_code=201)
    def attest(body: dict = Body(...), who=Depends(principal)):
        return plane.attest(who, body)

    @app.post("/v1/tenants/{tenant}/plans", status_code=201)
    def compile_plan(body: dict = Body(...), who=Depends(principal)):
        result = plane.compile(who, PlanRequest.from_document(body))
        if result["status"] != "COMPILED":
            return JSONResponse(result, status_code=422)
        return result

    @app.post("/v1/tenants/{tenant}/applications/{application}/plans/{plan}/approvals",
              status_code=201)
    def approve(application: str, plan: str, body: dict = Body(...), who=Depends(principal)):
        if set(body) - {"approval_id", "purpose", "valid_minutes"} or "approval_id" not in body:
            raise ContractError("Body is {approval_id[, purpose, valid_minutes]}")
        minutes = body.get("valid_minutes", 60)
        if type(minutes) is not int:
            raise ContractError("valid_minutes must be an integer")
        return plane.approve(who, application, plan, body["approval_id"],
                             purpose=body.get("purpose", "migration_phase"),
                             valid_for=timedelta(minutes=minutes))

    @app.post("/v1/tenants/{tenant}/applications/{application}/plans/{plan}/failback-requests")
    def require_failback(application: str, plan: str, body: dict = Body(...), who=Depends(principal)):
        if set(body) != {"reason"} or type(body["reason"]) is not str:
            raise ContractError("Body is {reason: supported reason code}")
        return plane.require_failback(who, application, plan, reason=body["reason"])

    @app.post("/v1/tenants/{tenant}/applications/{application}/plans/{plan}/failback-plans",
              status_code=201)
    def plan_failback(application: str, plan: str, body: dict = Body(...), who=Depends(principal)):
        result = plane.plan_failback(who, application, plan, PlanRequest.from_document(body))
        if result["status"] != "COMPILED":
            return JSONResponse(result, status_code=422)
        return result

    @app.post("/v1/tenants/{tenant}/applications/{application}/admissions", status_code=201)
    def admit(application: str, body: dict = Body(...), who=Depends(principal)):
        if set(body) != {"approval_id"}:
            raise ContractError("Body is {approval_id}")
        return plane.admit(who, application, body["approval_id"])

    @app.get("/v1/tenants/{tenant}/applications/{application}/units/{unit}")
    def status(application: str, unit: str, who=Depends(principal)):
        return plane.status(who, application, unit)

    @app.get("/v1/tenants/{tenant}/applications/{application}/evidence")
    def evidence(application: str, who=Depends(principal)):
        return plane.evidence(who, application)

    @app.get("/v1/tenants/{tenant}/applications/{application}/observations/{digest}")
    def observation(application: str, digest: str, who=Depends(principal)):
        return plane.observation(who, application, digest)

    @app.post("/v1/tenants/{tenant}/applications/{application}/plans/{plan}/abort")
    def abort(application: str, plan: str, body: dict = Body(...), who=Depends(principal)):
        if set(body) != {"target_inactive_verified"} or type(
                body["target_inactive_verified"]) is not bool:
            raise ContractError("Body is {target_inactive_verified: bool}")
        return plane.abort(who, application, plan,
                           target_inactive_verified=body["target_inactive_verified"])

    return app
