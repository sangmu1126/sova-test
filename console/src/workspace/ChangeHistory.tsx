import { useEffect, useState } from "react";
import { api } from "./auth";
import type { Identity } from "./auth";

type Entry = {
  event_id: string;
  kind: string;
  environment_id: string | null;
  subject: string | null;
  observed_at: string;
  recorded_at: string;
  collector_id: string;
};
type History = {
  version: 1;
  tenant_id: string;
  application_id: string;
  observational_only: true;
  authorizes_execution: false;
  coverage: "UNKNOWN";
  items: Entry[];
  next_after: string | null;
};
const kinds = new Set([
  "resource",
  "schema",
  "policy",
  "identity",
  "code",
  "vulnerability",
  "verifier",
  "adapter",
  "contract",
  "manifest",
  "qualification",
  "environment_profile",
  "dependency_snapshot",
  "unclassified",
]);
const text = (v: unknown): v is string =>
  typeof v === "string" && v.length > 0 && v.length <= 256;
const instant = (v: unknown): v is string =>
  typeof v === "string" &&
  /(?:Z|[+-]\d{2}:\d{2})$/.test(v) &&
  Number.isFinite(Date.parse(v));
export function checkedHistory(
  raw: unknown,
  tenant: string,
  app: string,
): History {
  const h = raw as History;
  if (
    !h ||
    h.version !== 1 ||
    h.tenant_id !== tenant ||
    h.application_id !== app ||
    h.observational_only !== true ||
    h.authorizes_execution !== false ||
    h.coverage !== "UNKNOWN" ||
    !Array.isArray(h.items) ||
    h.items.length > 20 ||
    !(
      h.next_after === null ||
      (typeof h.next_after === "string" &&
        h.next_after.length > 0 &&
        h.next_after.length <= 2048)
    ) ||
    (h.next_after !== null && h.items.length !== 20)
  )
    throw new Error("변경 기록의 범위를 확인하지 못했습니다.");
  const seen = new Set<string>();
  for (const e of h.items) {
    if (
      !e ||
      !text(e.event_id) ||
      seen.has(e.event_id) ||
      !kinds.has(e.kind) ||
      !text(e.collector_id) ||
      !(e.environment_id === null || text(e.environment_id)) ||
      !(e.subject === null || text(e.subject)) ||
      !instant(e.observed_at) ||
      !instant(e.recorded_at)
    )
      throw new Error("변경 기록의 형식을 확인하지 못했습니다.");
    seen.add(e.event_id);
  }
  return h;
}

export function ChangeHistory({
  application,
  identity,
}: {
  application: string;
  identity: Identity;
}) {
  const [value, setValue] = useState<History | null>(null);
  const [error, setError] = useState("");
  const [after, setAfter] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    setAfter("");
  }, [application, identity]);
  useEffect(() => {
    let active = true;
    setValue(null);
    setError("");
    if (!application || !identity.roles.includes("auditor")) return;
    void api(
      `/applications/${encodeURIComponent(application)}/change-history?after=${encodeURIComponent(after)}`,
    )
      .then((raw) => {
        const result = checkedHistory(raw, identity.tenant_id, application);
        if (active) setValue(result);
      })
      .catch(() => {
        if (active) {
          setValue(null);
          setError("변경 기록 조회 실패 · 현재 상태는 미확인입니다.");
        }
      });
    return () => {
      active = false;
    };
  }, [application, identity, after, refresh]);
  return (
    <section className="native-card">
      <h2>
        수집된 변경 기록 <small>Collected change history</small>
      </h2>
      <p>
        등록된 앱의 저장 이력입니다. 기록이 없거나 최근 기록이 있어도 전체 변경
        수집·현재 정상 상태·실행 허가를 뜻하지 않습니다.
      </p>
      <p>
        <strong>수집 범위 완전성: 미확인 (UNKNOWN)</strong> · 계획별 영향과 실행
        가능 여부는 기존 게이트에서 별도로 확인합니다.
      </p>
      <button
        className="button secondary"
        disabled={!application || !identity.roles.includes("auditor")}
        onClick={() => {
          setAfter("");
          setRefresh((v) => v + 1);
        }}
      >
        최신 기록 조회
      </button>
      {!identity.roles.includes("auditor") && (
        <p>변경 기록을 보려면 감사 조회 권한이 필요합니다.</p>
      )}
      {error && <p role="alert">{error}</p>}
      {value && identity.roles.includes("auditor") && (
        <>
          <p>
            DB 저장 시각 내림차순 · 페이지당 최대 20건. 새로 들어온 기록은 최신
            조회로 확인합니다. 관측 시각은 공급자가 관측한 시각이며 유효 기한이
            아닙니다.
          </p>
          {value.items.length === 0 ? (
            <p>이 앱의 저장 기록 없음 · 변경 없음으로 판정하지 않습니다.</p>
          ) : (
            <table className="workspace-observations-table">
              <thead>
                <tr>
                  <th>종류·환경</th>
                  <th>구성 요소</th>
                  <th>관측 시각 / 저장 시각</th>
                  <th>수집기·기록 ID</th>
                </tr>
              </thead>
              <tbody>
                {value.items.map((e) => (
                  <tr key={e.event_id}>
                    <td>
                      {e.kind}
                      <br />
                      {e.environment_id || "환경 미지정"}
                    </td>
                    <td>{e.subject || "대상 미확인"}</td>
                    <td>
                      {e.observed_at}
                      <br />
                      {e.recorded_at}
                    </td>
                    <td>
                      <code>{e.collector_id}</code>
                      <br />
                      <code>{e.event_id}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {value.next_after && (
            <button
              className="button secondary"
              onClick={() => setAfter(value.next_after!)}
            >
              이전 기록 20건
            </button>
          )}
        </>
      )}
    </section>
  );
}
