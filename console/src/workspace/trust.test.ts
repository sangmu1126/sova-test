import { describe, expect, it } from "vitest";
import { checkedTrustReceipt } from "./trust";

function receipt() {
  return {
    request_digest: "a".repeat(64),
    request: {
      tenant_id: "tenant",
      application_id: "orders",
      request_id: "request-1",
      requested_by: "requester",
      reason: "Exact application key",
      expires_at: "2026-11-20T01:00:00Z",
      expected_revision: 0,
      trust: {
        issuer_id: "issuer",
        key_id: "key",
        public_key_hex: "b".repeat(64),
        valid_until: "2026-11-20T02:00:00Z",
        revoked: false,
        allowed_scopes: [
          {
            tenant_id: "tenant",
            application_id: "orders",
            environment_id: "target",
            resource_id: "unit",
          },
        ],
        allowed_verifiers: ["verifier-1"],
      },
    },
    approval: null as null | {
      request_digest: string;
      approved_by: string;
      publication_revision: number;
      publication_digest: string;
    },
  };
}

describe("verifier request display binding", () => {
  it("displays a bounded pending request for exactly the selected scope", () => {
    expect(
      checkedTrustReceipt(receipt(), "tenant", "orders", "request-1").approval,
    ).toBeNull();
  });
  it.each([
    ["other", "orders", "request-1"],
    ["tenant", "other", "request-1"],
    ["tenant", "orders", "other"],
  ])("rejects a response from a previous selection", (tenant, app, id) => {
    expect(() => checkedTrustReceipt(receipt(), tenant, app, id)).toThrow();
  });
  it("refuses a nested key scope from another application", () => {
    const value = receipt();
    value.request.trust.allowed_scopes[0].application_id = "other";
    expect(() =>
      checkedTrustReceipt(value, "tenant", "orders", "request-1"),
    ).toThrow();
  });
  it("never displays self approval as a recorded independent publication", () => {
    const value = receipt();
    value.approval = {
      request_digest: value.request_digest,
      approved_by: "requester",
      publication_revision: 1,
      publication_digest: "c".repeat(64),
    };
    expect(() =>
      checkedTrustReceipt(value, "tenant", "orders", "request-1"),
    ).toThrow();
  });
  it("rejects approval for a different reviewed digest", () => {
    const value = receipt();
    value.approval = {
      request_digest: "d".repeat(64),
      approved_by: "approver",
      publication_revision: 1,
      publication_digest: "c".repeat(64),
    };
    expect(() =>
      checkedTrustReceipt(value, "tenant", "orders", "request-1"),
    ).toThrow();
  });
  it("does not turn an absent approval field into an approvable pending state", () => {
    const value: Record<string, unknown> = receipt();
    delete value.approval;
    expect(() =>
      checkedTrustReceipt(value, "tenant", "orders", "request-1"),
    ).toThrow();
  });
});
