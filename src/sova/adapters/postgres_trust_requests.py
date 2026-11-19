"""Atomic app-scoped key publication with authenticated request/approval history.

Reuses the security package's registry and its CAS. The borrowed connection
keeps publication and its approval receipt in ONE outer transaction.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib

from psycopg.types.json import Jsonb

from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.domain.contracts import ContractError, canonical_json, identifier, utc
from sova.domain.trust_requests import VerifierTrustRequest
from sova.domain.verifier_registry import VerifierTrustChange
from sova.ports.plans import RepositoryConflict, RecordNotFound
from sova_security.adapters.postgres_session import ScopedPostgresSession
from sova_security.adapters.verifier_registry_codec import decode_trust_change


def fingerprint(document):
    return hashlib.sha256(canonical_json(document, limit=131072).encode()).hexdigest()


class PostgresTrustRequests:
    def __init__(self, connect, *, tenant_id, application_id):
        self.tenant_id, self.application_id = identifier(tenant_id), identifier(application_id)
        self.session = ScopedPostgresSession(connect, tenant_id=tenant_id)

    def _lock(self, conn, request_id):
        key = canonical_json({"tenant": self.tenant_id, "application": self.application_id, "request": request_id})
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))

    def _request(self, conn, request_id):
        row = conn.execute("SELECT digest,document FROM sova_control.verifier_requests "
            "WHERE tenant_id=%s AND application_id=%s AND request_id=%s",
            (self.tenant_id, self.application_id, request_id)).fetchone()
        if row is None:
            raise RecordNotFound("Verifier request not found")
        request = VerifierTrustRequest.from_document(row[1])
        if (request.tenant_id, request.application_id, request.request_id, request.fingerprint) != (
                self.tenant_id, self.application_id, request_id, row[0]):
            raise ContractError("Verifier request integrity/scope mismatch")
        return request

    @contextmanager
    def _borrow(self, conn):
        yield conn  # inner security registry may use a savepoint, never commit/close outer

    def _registry(self, conn):
        return PostgresVerifierRegistry(lambda: self._borrow(conn), tenant_id=self.tenant_id,
                                         application_id=self.application_id)

    def _approval(self, conn, request):
        row = conn.execute("SELECT digest,document FROM sova_control.verifier_request_approvals "
            "WHERE tenant_id=%s AND application_id=%s AND request_id=%s",
            (self.tenant_id, self.application_id, request.request_id)).fetchone()
        if row is None:
            return None
        document = row[1]
        expected = {"schema_version", "tenant_id", "application_id", "request_id", "request_digest", "approval_id",
                    "approved_by", "approved_at", "approval_until", "publication_revision", "publication_digest"}
        if type(document) is not dict or set(document) != expected or fingerprint(document) != row[0]:
            raise ContractError("Verifier approval integrity mismatch")
        if (document["schema_version"] != 1 or type(document["schema_version"]) is not int
                or (document["tenant_id"], document["application_id"], document["request_id"], document["request_digest"]) !=
                    (self.tenant_id, self.application_id, request.request_id, request.fingerprint)):
            raise ContractError("Verifier approval binding mismatch")
        change = self._change(request, document["approved_by"], utc(datetime.fromisoformat(document["approved_at"])))
        if (document["publication_revision"], document["publication_digest"], document["approval_until"]) != (
                change.revision, change.fingerprint, change.approval_until.isoformat()):
            raise ContractError("Verifier publication receipt mismatch")
        event = conn.execute("SELECT digest,document FROM sova_control.verifier_trust_events "
            "WHERE tenant_id=%s AND application_id=%s AND issuer_id=%s AND key_id=%s AND revision=%s",
            (self.tenant_id, self.application_id, request.trust.issuer_id, request.trust.key_id, change.revision)).fetchone()
        if event is None or event[0] != change.fingerprint or decode_trust_change(event[1]) != change:
            raise ContractError("Verifier approval has no matching immutable publication")
        return document

    def get(self, request_id):
        identifier(request_id)
        with self.session.transaction() as conn:
            request = self._request(conn, request_id)
            return {"request": request.document, "request_digest": request.fingerprint,
                    "approval": self._approval(conn, request)}

    def create(self, request):
        if not isinstance(request, VerifierTrustRequest) or (request.tenant_id, request.application_id) != (
                self.tenant_id, self.application_id):
            raise ContractError("Typed same-application verifier request required")
        with self.session.transaction() as conn:
            self._lock(conn, request.request_id)
            try:
                existing = self._request(conn, request.request_id)
            except RecordNotFound:
                existing = None
            if existing is not None:
                # Lost-response retry returns the old expiry; it never renews a request.
                a, b = existing.document.copy(), request.document.copy()
                for name in ("requested_at", "expires_at"):
                    a.pop(name)
                    b.pop(name)
                if a != b:
                    raise RepositoryConflict("Verifier request ID already binds another request")
                return {"request": existing.document, "request_digest": existing.fingerprint,
                        "approval": self._approval(conn, existing)}
            request.require_current(conn.execute("SELECT clock_timestamp()").fetchone()[0])
            try:
                revision = self._registry(conn).latest(request.trust.issuer_id, request.trust.key_id).revision
            except RecordNotFound:
                revision = 0
            if revision != request.expected_revision:
                raise RepositoryConflict("Verifier trust revision changed")
            conn.execute("INSERT INTO sova_control.verifier_requests(tenant_id,application_id,request_id,digest,document) "
                "VALUES(%s,%s,%s,%s,%s)", (self.tenant_id, self.application_id, request.request_id,
                request.fingerprint, Jsonb(request.document)))
            return {"request": request.document, "request_digest": request.fingerprint, "approval": None}

    @staticmethod
    def _change(request, actor, now):
        request.require_current(now)
        if actor == request.requested_by:
            raise ContractError("Independent verifier trust approver required")
        until = request.expires_at if request.trust.revoked else min(request.expires_at, request.trust.valid_until)
        return VerifierTrustChange(request.tenant_id, request.application_id, request.expected_revision + 1,
            request.trust, request.requested_by, actor, request.reason, now, until)

    def approve(self, request_id, *, approval_id, actor):
        for value in (request_id, approval_id, actor):
            identifier(value)
        with self.session.transaction() as conn:
            self._lock(conn, request_id)
            request = self._request(conn, request_id)
            existing = self._approval(conn, request)
            if existing is not None:
                if (existing["approval_id"], existing["approved_by"]) != (approval_id, actor):
                    raise RepositoryConflict("Verifier request already approved by another action")
                return existing
            now = utc(conn.execute("SELECT clock_timestamp()").fetchone()[0])
            change = self._change(request, actor, now)
            self._registry(conn).publish(change)
            document = {"schema_version": 1, "tenant_id": self.tenant_id, "application_id": self.application_id,
                "request_id": request_id, "request_digest": request.fingerprint, "approval_id": approval_id,
                "approved_by": actor, "approved_at": now.isoformat(), "approval_until": change.approval_until.isoformat(),
                "publication_revision": change.revision, "publication_digest": change.fingerprint}
            inserted = conn.execute("INSERT INTO sova_control.verifier_request_approvals "
                "(tenant_id,application_id,request_id,approval_id,digest,document) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                (self.tenant_id, self.application_id, request_id, approval_id, fingerprint(document), Jsonb(document))).fetchone()
            if inserted is None:
                raise RepositoryConflict("Approval ID already belongs to another verifier request")
            return document
