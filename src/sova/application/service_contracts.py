"""Synthetic requirements for exercising contract input; no resource claims."""

from sova.domain.service_contracts import ServiceContract


def example_service_contract():
    return ServiceContract.from_document(
        {
            "apiVersion": "migration.platform/v1alpha1",
            "kind": "ServiceContract",
            "metadata": {
                "tenant_id": "org-example",
                "id": "orders-service",
                "revision": 1,
            },
            "spec": {
                "owner": "team-orders",
                "acknowledgment_semantics": {
                    "accepted": "durable_request_record",
                    "completed": "transaction_committed_and_result_recorded",
                },
                "business": {
                    "invariants": [
                        {
                            "id": "ORDER-01",
                            "description": "동일 고객·멱등 키의 주문 효과를 한 번만 확정한다.",
                        }
                    ],
                    "external_effects": [],
                },
                "migration": {
                    "profile": "PLANNED_SINGLE_AUTHORITY",
                    "units": {"mode": "whole_service"},
                },
                "data": {
                    "protected_sets": ["orders", "work_items", "idempotency_results"],
                    "preserve_completed_operations": True,
                    "comparison_contract_ref": "orders-consistency-v1",
                },
                "availability": {
                    "max_write_admission_pause_ms": 5000,
                    "max_client_completion_wait_ms": 15000,
                    "latency_budget_ref": "orders-slo-v1",
                    "read_consistency_during_handoff": "read_your_writes_or_explicit_retry",
                },
                "security": {
                    "required_controls": [
                        "deny_cross_tenant_data_access",
                        "verified_transport_encryption",
                        "tested_restore",
                    ]
                },
                "residency": {
                    "business_data": ["approved-zone-a"],
                    "backups": ["approved-zone-a"],
                    "audit": ["approved-zone-a"],
                    "ai_inputs": [],
                },
                "operations": {
                    "restore_test_required": True,
                    "source_retirement_requires_separate_approval": True,
                },
                "approval": {
                    "separation_of_duties": True,
                    "high_risk_handoff": "human_approval",
                },
                "exit": {
                    "remove": ["source-db"],
                    "allowed_remaining": ["customer-owned-idp"],
                },
            },
        }
    )
