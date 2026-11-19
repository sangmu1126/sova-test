export type TrustReceipt = {
  request_digest: string;
  request: {
    tenant_id: string;
    application_id: string;
    request_id: string;
    requested_by: string;
    reason: string;
    expires_at: string;
    expected_revision: number;
    trust: {
      issuer_id: string;
      key_id: string;
      public_key_hex: string;
      valid_until: string;
      revoked: boolean;
      allowed_scopes: {
        tenant_id: string;
        application_id: string;
        environment_id: string;
        resource_id: string;
      }[];
      allowed_verifiers: string[];
    };
  };
  approval: {
    request_digest: string;
    approved_by: string;
    publication_revision: number;
    publication_digest: string;
  } | null;
};

export function checkedTrustReceipt(
  value: unknown,
  tenant: string,
  app: string,
  id: string,
): TrustReceipt {
  const receipt = value as TrustReceipt;
  const request = receipt?.request;
  if (
    !request ||
    typeof request.requested_by !== "string" ||
    typeof request.reason !== "string" ||
    typeof request.expires_at !== "string" ||
    typeof request.trust?.revoked !== "boolean" ||
    typeof request.trust?.issuer_id !== "string" ||
    typeof request.trust?.key_id !== "string" ||
    !Array.isArray(request.trust?.allowed_verifiers) ||
    request.trust.allowed_verifiers.length === 0 ||
    request.trust.allowed_verifiers.some(
      (value) => typeof value !== "string" || !value,
    ) ||
    !(
      receipt.approval === null ||
      (typeof receipt.approval === "object" && receipt.approval !== undefined)
    ) ||
    request.tenant_id !== tenant ||
    request.application_id !== app ||
    request.request_id !== id ||
    !/^[0-9a-f]{64}$/.test(receipt.request_digest) ||
    !Number.isSafeInteger(request.expected_revision) ||
    request.expected_revision < 0 ||
    !Number.isFinite(Date.parse(request.expires_at)) ||
    !request.requested_by ||
    !/^[0-9a-f]{64}$/.test(request.trust?.public_key_hex) ||
    !Array.isArray(request.trust.allowed_scopes) ||
    request.trust.allowed_scopes.length === 0 ||
    request.trust.allowed_scopes.some(
      (scope) => scope.tenant_id !== tenant || scope.application_id !== app,
    )
  )
    throw new Error("선택한 앱에 연결된 검증자 키 요청을 확인할 수 없습니다.");
  const approval = receipt.approval;
  if (
    approval &&
    (typeof approval.approved_by !== "string" ||
      !Number.isSafeInteger(approval.publication_revision) ||
      approval.request_digest !== receipt.request_digest ||
      approval.approved_by === request.requested_by ||
      !approval.approved_by ||
      !/^[0-9a-f]{64}$/.test(approval.publication_digest) ||
      approval.publication_revision !== request.expected_revision + 1)
  )
    throw new Error("키 발행 승인 기록의 결속을 확인할 수 없습니다.");
  return receipt;
}
