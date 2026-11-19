"""Verifier publication cannot borrow a plan approver or caller-written identity."""

from datetime import UTC, datetime, timedelta

import pytest

from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.trust_requests import VerifierTrustRequest, new_request
from sova.domain.verifier_registry import trust_document
from sova.service.control_plane import ControlPlane, Forbidden, Principal, Role

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def body():
    trust = VerifierTrust("registry-producer", "key-1", "ab" * 32,
        (ResourceScope("tenant-a", "orders", "sova-contract-registry", "service-contract"),),
        ("registry-attestor-1",), NOW - timedelta(minutes=1), NOW + timedelta(hours=1))
    return {"expected_revision": 0, "reason": "Initial exact-app verifier lease", "trust": trust_document(trust)}


def request(document=None):
    return new_request(tenant="tenant-a", application="orders", request_id="request-1",
        document=body() if document is None else document, actor="authenticated-requester", now=NOW)


def test_request_roundtrip_has_server_identity_and_bounded_lifetime():
    value = request()
    assert value.requested_by == "authenticated-requester"
    assert value.expires_at == NOW + timedelta(minutes=15)
    assert VerifierTrustRequest.from_document(value.document) == value


@pytest.mark.parametrize("field,value", [("approved_by", "invented-approver"),
    ("requested_by", "another-user"), ("requested_at", NOW.isoformat()), ("expires_at", "2099-01-01"),
    ("approval_id", "pre-approved"), ("private_key_hex", "11" * 32)])
def test_caller_cannot_supply_authority_or_server_time(field, value):
    document = body()
    document[field] = value
    with pytest.raises(ContractError, match="server-derived"):
        request(document)


@pytest.mark.parametrize("change", ["tenant", "application", "private_key", "boolean_revision", "long_lease"])
def test_wrong_scope_hidden_key_and_unbounded_lease_are_refused(change):
    document = body()
    if change in ("tenant", "application"):
        document["trust"]["allowed_scopes"][0][change + "_id"] = "other"
    elif change == "private_key":
        document["trust"]["private_key"] = "not-accepted"
    elif change == "boolean_revision":
        document["expected_revision"] = True
    else:
        document["trust"]["valid_until"] = (NOW + timedelta(hours=8, microseconds=1)).isoformat()
    with pytest.raises(ContractError):
        request(document)


def test_request_is_expired_at_the_boundary_and_never_renewed_by_a_late_approval():
    value = request()
    value.require_current(value.expires_at - timedelta(microseconds=1))
    with pytest.raises(ContractError):
        value.require_current(value.expires_at)
    with pytest.raises(ContractError):
        value.require_current(NOW - timedelta(microseconds=1))


def test_revocation_of_an_expired_grant_can_still_be_requested():
    document = body()
    document["trust"]["valid_until"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(ContractError):
        request(document)
    document["trust"]["revoked"] = True
    request(document).require_current(NOW)


@pytest.mark.parametrize("role", [Role.REGISTRAR, Role.ATTESTOR, Role.PLANNER, Role.APPROVER, Role.OPERATOR, Role.AUDITOR])
def test_existing_business_roles_are_not_automatically_trust_administrators(role):
    plane = object.__new__(ControlPlane)
    principal = Principal("business-user", "tenant-a", frozenset({role}))
    with pytest.raises(Forbidden):
        plane.request_verifier_trust(principal, "orders", "request-1", body())
    with pytest.raises(Forbidden):
        plane.approve_verifier_trust(principal, "orders", "request-1", {"approval_id": "approval-1", "request_digest": "a" * 64})


def test_self_approval_is_forbidden_even_if_both_security_roles_were_assigned(monkeypatch):
    plane = object.__new__(ControlPlane)

    class Store:
        def get(self, request_id):
            return {"request": {"requested_by": "requester"}}

        def approve(self, *args, **kwargs):
            raise AssertionError("Self approval reached the publisher")

    monkeypatch.setattr(plane, "_trust_requests", lambda tenant, application: Store())
    principal = Principal("requester", "tenant-a", frozenset({Role.TRUST_REQUESTER, Role.TRUST_APPROVER}))
    with pytest.raises(Forbidden):
        plane.approve_verifier_trust(principal, "orders", "request-1", {"approval_id": "approval-1", "request_digest": "a" * 64})


def test_service_approver_identity_is_passed_from_the_principal(monkeypatch):
    plane = object.__new__(ControlPlane)
    calls = []

    class Store:
        def get(self, request_id):
            return {"request": {"requested_by": "different-requester"}}

        def approve(self, request_id, **arguments):
            calls.append(arguments)
            return {"recorded": True}

    monkeypatch.setattr(plane, "_trust_requests", lambda tenant, application: Store())
    principal = Principal("verified-security-approver", "tenant-a", frozenset({Role.TRUST_APPROVER}))
    plane.approve_verifier_trust(principal, "orders", "request-1", {"approval_id": "approval-1", "request_digest": "a" * 64})
    assert calls == [{"approval_id": "approval-1", "actor": principal.subject, "expected_digest": "a" * 64}]
    with pytest.raises(ContractError):
        plane.approve_verifier_trust(principal, "orders", "request-1",
                                     {"approval_id": "approval-1", "approved_by": "injected"})
