import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { request } from "../../api";
import { nativeStatus, nextSteps, reasons } from "./status";
import type { NativeStatus } from "./status";
import "./native.css";

export function NativePlanView({ data }: { data: NativeStatus }) {
  const recovery = data.recovery;
  return (
    <>
      <section
        className={`native-recovery ${recovery.status}`}
        aria-live="polite"
      >
        <p className="eyebrow">EFFECT JOURNAL · READ ONLY</p>
        <h2>
          {recovery.required === true
            ? "실행 보류 · 운영 검토 필요"
            : recovery.required === false
              ? "현재 관측에서 복구 보류 없음"
              : "운영 상태 미확인"}
        </h2>
        <p>
          {recovery.reason
            ? reasons[recovery.reason]
            : "이 표시는 실행 허가나 서비스 이전 완료 판정이 아닙니다."}
        </p>
        <p>
          미확정 명령은 자동으로 다시 실행하지 않습니다. 재승인만으로 원격
          결과의 불확실성이 해소되지는 않습니다.
        </p>
      </section>
      <div className="native-facts">
        <article className="native-card">
          <small>저장된 권한 상태</small>
          <h2>{data.state}</h2>
          <p>관측 보류와 별도로 저장된 원본 상태입니다.</p>
        </article>
        <article className="native-card">
          <small>업무 권한 환경</small>
          <h2>{data.owner_environment}</h2>
          <p>
            epoch {data.epoch} · 다음 작업 번호 {data.next_operation}
          </p>
        </article>
        <article className="native-card">
          <small>대상 쓰기 활성화 가능성 기록</small>
          <h2>
            {data.activation_possible ? "가능성 있음" : "가능성 기록 없음"}
          </h2>
          <p>실제 DB 접속 시험 결과를 대신하지 않습니다.</p>
        </article>
      </div>
      <section className="native-card native-binding">
        <h2>관측 범위</h2>
        <dl>
          {Object.entries(data.scope).map(([name, value]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd>{value || "미확인"}</dd>
            </div>
          ))}
        </dl>
        <p>
          관측{" "}
          {new Date(data.observed_at).toLocaleTimeString("ko-KR", {
            hour12: false,
          })}{" "}
          · 유효 기한{" "}
          {new Date(data.expires_at).toLocaleTimeString("ko-KR", {
            hour12: false,
          })}
        </p>
      </section>
      <section className="native-card">
        <h2>미확정 효과 {recovery.pending_effects.length}개</h2>
        {recovery.pending_effects.length ? (
          <div className="native-table">
            <table>
              <thead>
                <tr>
                  <th>명령</th>
                  <th>원장 상태</th>
                  <th>revision</th>
                  <th>명령 지문</th>
                </tr>
              </thead>
              <tbody>
                {recovery.pending_effects.map((e) => (
                  <tr key={e.fingerprint}>
                    <td>{e.action}</td>
                    <td>{e.status}</td>
                    <td>{e.revision}</td>
                    <td>
                      <code>{e.fingerprint}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p>
            {recovery.status === "unavailable"
              ? "목록을 확인하지 못했습니다. 0건 검증으로 해석하지 않습니다."
              : "현재 관측 범위에 미확정 효과가 없습니다."}
          </p>
        )}
      </section>
      {recovery.next_steps.length > 0 && (
        <section className="native-card">
          <h2>운영자가 확인할 순서</h2>
          <ol>
            {recovery.next_steps.map((step) => (
              <li key={step}>{nextSteps[step]}</li>
            ))}
          </ol>
          <p>이 화면에서는 복구·재실행·쓰기 재개를 요청할 수 없습니다.</p>
        </section>
      )}
    </>
  );
}

export function NativePlanPanel() {
  const [data, setData] = useState<NativeStatus | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(Date.now());
  const [pending, setPending] = useState(false);
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    const timeout = setTimeout(() => controller.abort(), 4500);
    setPending(true);
    try {
      const next = nativeStatus(
        await request<unknown>(
          "/api/native/status",
          undefined,
          controller.signal,
        ),
      );
      if (!controller.signal.aborted) {
        setData(next);
        setError("");
      }
    } catch {
      if (active.current === controller) {
        setData(null);
        setError(
          "현재 원장을 읽지 못했습니다. 등록·로그인·연결 상태를 확인하세요.",
        );
      }
    } finally {
      clearTimeout(timeout);
      if (active.current === controller) {
        active.current = null;
        setPending(false);
      }
    }
  }, []);
  useEffect(() => {
    void refresh();
    const polling = setInterval(() => void refresh(), 2000);
    const clock = setInterval(() => setTick(Date.now()), 250);
    return () => {
      clearInterval(polling);
      clearInterval(clock);
      active.current?.abort();
      active.current = null;
    };
  }, [refresh]);
  const fresh = data && tick < Date.parse(data.expires_at);
  return (
    <div className="native-plan">
      <section className="page-heading">
        <div>
          <p className="eyebrow">CONTRACT PLAN · OBSERVATION</p>
          <h1>계약 계획의 운영 상태</h1>
          <p>현재 계획의 권한 상태와 효과 원장을 함께 읽습니다.</p>
        </div>
        <button
          className="button secondary"
          disabled={pending}
          onClick={() => void refresh()}
        >
          <RefreshCw size={15} className={pending ? "spin" : ""} />
          원장 새로고침
        </button>
      </section>
      <p className="native-notice">
        고정 등록된 네이티브 제어면의 읽기 전용 조회입니다. 기존 무상태 시험
        화면과 별도 경로이며, 이 관측은 실행을 승인하지 않습니다.
      </p>
      {fresh ? (
        <NativePlanView data={data} />
      ) : (
        <section className="native-recovery unavailable" role="status">
          <h2>
            {pending && !error && !data
              ? "현재 원장 조회 중"
              : "운영 상태 미확인"}
          </h2>
          <p>
            {error ||
              "현재 유효한 관측이 없습니다. 만료된 결과는 표시하지 않습니다."}
          </p>
        </section>
      )}
    </div>
  );
}
