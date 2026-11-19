import { useEffect, useState } from "react";
import { api } from "./auth";
import type { Identity } from "./auth";

type Frame = {
  state: "fresh" | "historical" | "unavailable";
  observed_at?: string;
  valid_until?: string;
  record_sha256?: string;
  samples?: {
    side: string;
    status: number;
    duration_ms: number;
    verified_application_https: boolean;
  }[];
  available?: boolean;
  amount?: string | null;
  currency?: string | null;
  estimated?: boolean | null;
};
type Observations = {
  version: number;
  tenant_id: string;
  application_id: string;
  observational_only: true;
  authorizes_execution: false;
  health: Frame;
  cost: Frame;
};
export function checkedObservations(
  raw: unknown,
  tenant: string,
  app: string,
): Observations {
  const value = raw as Observations;
  if (
    !value ||
    value.version !== 1 ||
    value.tenant_id !== tenant ||
    value.application_id !== app ||
    value.observational_only !== true ||
    value.authorizes_execution !== false
  )
    throw new Error("관측 응답의 앱·범위를 확인하지 못했습니다.");
  for (const frame of [value.health, value.cost]) {
    if (!frame || !["fresh", "historical", "unavailable"].includes(frame.state))
      throw new Error("관측 상태가 올바르지 않습니다.");
    if (
      frame.state !== "unavailable" &&
      (!Number.isFinite(Date.parse(frame.observed_at || "")) ||
        !Number.isFinite(Date.parse(frame.valid_until || "")) ||
        Date.parse(frame.valid_until!) <= Date.parse(frame.observed_at!) ||
        !/^[a-f0-9]{64}$/.test(frame.record_sha256 || ""))
    )
      throw new Error("관측의 시각·기록 식별자가 올바르지 않습니다.");
  }
  if (
    value.health.state !== "unavailable" &&
    (!Array.isArray(value.health.samples) ||
      value.health.samples.length < 1 ||
      value.health.samples.length > 64 ||
      value.health.samples.some(
        (s) =>
          !["source", "target"].includes(s.side) ||
          !Number.isInteger(s.status) ||
          s.status < 100 ||
          s.status > 599 ||
          !Number.isFinite(s.duration_ms) ||
          s.duration_ms < 0 ||
          s.duration_ms > 60000 ||
          typeof s.verified_application_https !== "boolean",
      ))
  )
    throw new Error("유효한 HTTP 표본이 아닙니다.");
  if (
    value.cost.available === true &&
    (typeof value.cost.amount !== "string" ||
      !/^-?\d+(\.\d+)?$/.test(value.cost.amount) ||
      value.cost.amount.length > 64 ||
      value.cost.currency !== "USD" ||
      (Number(value.cost.amount) === 0 && value.cost.estimated !== false))
  )
    throw new Error("확인되지 않은 비용입니다.");
  return value;
}

export function OperationalPanel({
  application,
  identity,
}: {
  application: string;
  identity: Identity;
}) {
  const [value, setValue] = useState<Observations | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [tick, setTick] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let active = true;
    setValue(null);
    setError("");
    if (!application || !identity.roles.includes("auditor")) return;
    async function load() {
      try {
        const result = checkedObservations(
          await api(
            `/applications/${encodeURIComponent(application)}/operational-observations`,
          ),
          identity.tenant_id,
          application,
        );
        if (active) {
          setValue(result);
          setError("");
        }
      } catch {
        if (active) {
          setValue(null);
          setError("관측을 조회하지 못했습니다. 현재 상태는 미확인입니다.");
        }
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [application, identity, refresh]);
  const label = (frame: Frame) =>
    frame.state === "unavailable"
      ? "미확인"
      : frame.state === "historical" ||
          tick >= Date.parse(frame.valid_until || "")
        ? "과거 기록"
        : "현재 표본";
  return (
    <section className="native-card">
      <h2>
        실측·비용 <small>Operational observations</small>
      </h2>
      <p>
        등록된 앱의 읽기 전용 관측입니다. 이 화면은 이전 완료나 실행 허가를
        판정하지 않습니다.
      </p>
      <button
        className="button secondary"
        disabled={!application || !identity.roles.includes("auditor")}
        onClick={() => setRefresh((v) => v + 1)}
      >
        관측 새로고침
      </button>
      {!identity.roles.includes("auditor") && (
        <p>이 앱 관측을 보려면 감사 조회 권한이 필요합니다.</p>
      )}
      {error && <p role="alert">{error}</p>}
      {value && (
        <>
          <h3>앱 HTTP 표본 · {label(value.health)}</h3>
          <p>
            관측 게이트웨이가 실제 앱 상태 URL을 호출한 표본입니다. 앱 내부 처리
            시간·전체 트래픽·서비스 SLO 측정은 포함하지 않습니다.
          </p>
          {value.health.state !== "unavailable" && (
            <>
              <p>
                관측 시각: {value.health.observed_at} · 유효 기한:{" "}
                {value.health.valid_until}
              </p>
              <table className="workspace-observations-table">
                <thead>
                  <tr>
                    <th>환경</th>
                    <th>HTTP</th>
                    <th>게이트웨이 지연</th>
                    <th>앱 TLS</th>
                  </tr>
                </thead>
                <tbody>
                  {value.health.samples?.map((s, i) => (
                    <tr key={i}>
                      <td>
                        {s.side === "source" ? "AWS 출발지" : "VM 목적지"}
                      </td>
                      <td>{s.status}</td>
                      <td>{s.duration_ms.toFixed(1)} ms</td>
                      <td>
                        {s.verified_application_https ? "검증" : "미확인"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p>
                기록 SHA-256: <code>{value.health.record_sha256}</code>
              </p>
            </>
          )}
          <h3>실제 청구 귀속 관측 · {label(value.cost)}</h3>
          <p>
            {value.cost.available === true
              ? `${value.cost.currency} ${value.cost.amount}${value.cost.estimated ? " (추정치)" : ""}`
              : "귀속 금액 미확인 · 0원으로 계산하지 않습니다."}
          </p>
          <p>
            등록된 AWS 실행 태그의 비용만 포함합니다. 미귀속 비용·로컬 VM의
            장비·전력·운영비는 포함하지 않으며, 총비용이나 최저 비용 환경을
            판정하지 않습니다.
          </p>
          {value.cost.observed_at && <p>관측 시각: {value.cost.observed_at}</p>}
        </>
      )}
    </section>
  );
}
