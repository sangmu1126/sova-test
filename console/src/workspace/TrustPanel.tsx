import { useEffect, useState } from "react";
import { api } from "./auth";
import type { Identity } from "./auth";
import { checkedTrustReceipt } from "./trust";
import type { TrustReceipt } from "./trust";

export function TrustPanel({
  application,
  identity,
}: {
  application: string;
  identity: Identity;
}) {
  const [id, setId] = useState("");
  const [document, setDocument] = useState("");
  const [receipt, setReceipt] = useState<TrustReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(Date.now());
  const has = (role: string) => identity.roles.includes(role);
  const path = `/applications/${encodeURIComponent(application)}/verifier-requests/${encodeURIComponent(id)}`;
  useEffect(() => {
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    setReviewed(false);
    try {
      setReceipt(
        checkedTrustReceipt(
          await action(),
          identity.tenant_id,
          application,
          id,
        ),
      );
    } catch (failure) {
      setReceipt(null);
      setError(
        failure instanceof Error
          ? failure.message
          : "키 요청을 처리하지 못했습니다.",
      );
    } finally {
      setBusy(false);
    }
  }
  const canApprove =
    !!receipt &&
    !receipt.approval &&
    has("trust_approver") &&
    receipt.request.requested_by !== identity.subject &&
    tick < Date.parse(receipt.request.expires_at);
  return (
    <section className="native-card">
      <h2>검증자 키 요청·승인</h2>
      <p>
        등록된 공개 키의 앱·자원·검증 범위를 다른 보안 승인자가 검토합니다. 일반
        계획 승인 권한으로는 키를 발행할 수 없습니다.
      </p>
      <label>
        검증자 키 요청 ID
        <input
          aria-label="검증자 키 요청 ID"
          value={id}
          disabled={busy}
          onChange={(event) => {
            setId(event.target.value);
            setReceipt(null);
            setReviewed(false);
          }}
        />
      </label>
      <label>
        공개 키 요청 JSON — 개인 키 제외
        <textarea
          aria-label="공개 키 요청 JSON"
          rows={8}
          maxLength={131072}
          value={document}
          disabled={busy}
          onChange={(event) => setDocument(event.target.value)}
        />
      </label>
      <div className="workspace-form">
        <button
          className="button secondary"
          disabled={
            busy || !application || !id || !has("trust_requester") || !document
          }
          onClick={() => void run(() => api(path, JSON.parse(document), "PUT"))}
        >
          검증자 키 요청 저장
        </button>
        <button
          className="button secondary"
          disabled={
            busy ||
            !application ||
            !id ||
            !(has("trust_requester") || has("trust_approver") || has("auditor"))
          }
          onClick={() => void run(() => api(path))}
        >
          검증자 키 요청 조회
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {receipt && (
        <div className="workspace-trust-binding">
          <p>
            요청 digest: <code>{receipt.request_digest}</code>
          </p>
          <p>
            요청자: {receipt.request.requested_by} · 만료:{" "}
            {receipt.request.expires_at}
          </p>
          <p>
            검증자: {receipt.request.trust.issuer_id} /{" "}
            {receipt.request.trust.key_id}
          </p>
          <p>
            공개 키: <code>{receipt.request.trust.public_key_hex}</code>
          </p>
          <p>사유: {receipt.request.reason}</p>
          <pre>{JSON.stringify(receipt.request.trust, null, 2)}</pre>
          {receipt.approval ? (
            <p>
              키 발행 승인 기록 · revision{" "}
              {receipt.approval.publication_revision} · 승인자{" "}
              {receipt.approval.approved_by}. 현재 서명 유효성과 실행 권한은
              서버가 매번 다시 확인합니다.
            </p>
          ) : (
            <>
              <label>
                <input
                  type="checkbox"
                  checked={reviewed}
                  disabled={busy || !canApprove}
                  onChange={(event) => setReviewed(event.target.checked)}
                />
                표시한 공개 키·앱·자원 범위와 기한을 검토했습니다.
              </label>
              <button
                className="button primary"
                disabled={busy || !canApprove || !reviewed}
                onClick={() =>
                  void run(async () => {
                    await api(path + "/approvals", {
                      approval_id: "trust-approval-" + crypto.randomUUID(),
                      request_digest: receipt.request_digest,
                    });
                    return api(path);
                  })
                }
              >
                표시한 검증자 키 발행 승인
              </button>
            </>
          )}
        </div>
      )}
    </section>
  );
}
