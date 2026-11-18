import { useCallback, useEffect, useRef, useState } from "react";
import {
  Layers3,
  FileCheck2,
  ShieldCheck,
  Activity,
  LogIn,
  LogOut,
  RefreshCw,
  ArrowRight,
  Upload,
} from "lucide-react";
import { api, initialize, login, logout, currentConfig } from "./auth";
import type { Identity } from "./auth";
import { NativePlanView } from "../features/native/NativePlanPanel";
import { nativeStatus } from "../features/native/status";
import type { NativeStatus } from "../features/native/status";

type Item = { subject_id: string; revision: number; digest: string };
type Page<T> = { items: T[]; next_after: string | null };
type Plan = {
  plan_id: string;
  plan_digest: string;
  created_by: string;
  valid_until: string;
  purpose: string;
};
type Detail = {
  plan_id: string;
  plan_digest: string;
  unit_digest: string;
  compiled_digest: string;
  plan: {
    created_by: string;
    valid_until: string;
    steps: {
      step_id: string;
      action: string;
      scope: { environment_id: string; resource_id: string };
      depends_on: string[];
    }[];
    obligations: unknown[];
  };
  compiled: {
    unit: { members: string[] };
    source_profile?: unknown;
    target_profile?: unknown;
  };
  approvals: {
    approval_id: string;
    revision: number;
    revoked: boolean;
    approved_by: string;
    expires_at: string;
  }[];
  authorizes_execution: false;
};
const kinds = [
  "service_contract",
  "dependency_snapshot",
  "environment_profile",
  "capability_manifest",
  "strategy_qualification",
];
const kindNames: Record<string, string> = {
  service_contract: "서비스 계약",
  dependency_snapshot: "의존성 조사",
  environment_profile: "환경 프로필",
  capability_manifest: "어댑터 능력",
  strategy_qualification: "방향별 검증 자격",
};
const segment = encodeURIComponent;

export function WorkspaceApp() {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState("plan");
  const [catalog, setCatalog] = useState<Record<string, Page<Item>>>({});
  const [application, setApplication] = useState("");
  const [plans, setPlans] = useState<Page<Plan>>({
    items: [],
    next_after: null,
  });
  const [planId, setPlanId] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [observation, setObservation] = useState<NativeStatus | null>(null);
  const [observedTick, setObservedTick] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("service_contract");
  const [subject, setSubject] = useState("");
  const [revision, setRevision] = useState(0);
  const [document, setDocument] = useState("");
  const [envelope, setEnvelope] = useState("");
  const [fields, setFields] = useState({
    snapshot_id: "",
    source_profile: "",
    target_profile: "",
    manifest: "",
    qualification: "",
    nodes: "",
    rows: "",
  });
  const [approvalId, setApprovalId] = useState("");
  const [confirmation, setConfirmation] = useState<{
    title: string;
    application: string;
    planId: string;
    digest: string;
    run: () => Promise<unknown>;
  } | null>(null);
  const context = useRef({ application, planId });
  context.current = { application, planId };
  const base = "/applications/" + segment(application);
  const has = (role: string) => identity?.roles.includes(role) || false;

  const refreshCatalog = useCallback(async () => {
    const pages = await Promise.all(
      kinds.map(
        async (k) => [k, await api<Page<Item>>("/registrations/" + k)] as const,
      ),
    );
    setCatalog(Object.fromEntries(pages));
  }, []);
  const refreshPlans = useCallback(async () => {
    if (!application) {
      setPlans({ items: [], next_after: null });
      return;
    }
    const next = await api<Page<Plan>>(
      "/applications/" + segment(application) + "/plans",
    );
    if (context.current.application === application) setPlans(next);
  }, [application]);
  const refreshDetail = useCallback(async () => {
    if (!application || !planId) {
      setDetail(null);
      setObservation(null);
      return;
    }
    const next = await api<Detail>(
      `/applications/${segment(application)}/plans/${segment(planId)}`,
    );
    if (next.authorizes_execution !== false || next.plan_id !== planId)
      throw new Error("계획 결속을 확인하지 못했습니다.");
    if (
      context.current.application !== application ||
      context.current.planId !== planId
    )
      return;
    setDetail(next);
    const state = await api<unknown>(
      `/applications/${segment(application)}/units/${segment(next.unit_digest)}`,
    );
    if (
      context.current.application !== application ||
      context.current.planId !== planId
    )
      return;
    if (
      state &&
      typeof state === "object" &&
      "state" in state &&
      state.state === null
    ) {
      setObservation(null);
      return;
    }
    const value = nativeStatus(state);
    if (
      value.scope.application_id !== application ||
      value.scope.plan_digest !== next.plan_digest ||
      value.scope.unit_digest !== next.unit_digest ||
      value.scope.tenant_id !== currentConfig()?.tenant_id
    )
      throw new Error("다른 계획의 관측은 표시하지 않습니다.");
    setObservation(value);
  }, [application, planId]);
  useEffect(() => {
    let mounted = true;
    void initialize()
      .then(async (user) => {
        if (user && !user.expired) {
          const person = await api<Identity>("/identity");
          if (person.tenant_id !== currentConfig()?.tenant_id)
            throw new Error("테넌트가 일치하지 않습니다.");
          if (mounted) {
            setIdentity(person);
            await refreshCatalog();
          }
        }
      })
      .catch(
        () =>
          mounted && setError("로그인 또는 서버 구성을 확인하지 못했습니다."),
      )
      .finally(() => mounted && setLoading(false));
    const expired = () => {
      setIdentity(null);
      setDetail(null);
      setObservation(null);
      setConfirmation(null);
      setError("로그인이 만료되었습니다. 다시 로그인하세요.");
    };
    window.addEventListener("sova-auth-expired", expired);
    return () => {
      mounted = false;
      window.removeEventListener("sova-auth-expired", expired);
    };
  }, [refreshCatalog]);
  useEffect(() => {
    setPlanId("");
    setDetail(null);
    setObservation(null);
    setApprovalId("");
    setConfirmation(null);
    void refreshPlans().catch(() => setError("계획 목록을 읽지 못했습니다."));
  }, [application, refreshPlans]);
  useEffect(() => {
    if (!identity || !planId) return;
    let stopped = false,
      running = false;
    const poll = async () => {
      if (running || stopped) return;
      running = true;
      try {
        await refreshDetail();
      } catch {
        if (!stopped) {
          setObservation(null);
          setError("현재 계획·관측을 확인하지 못했습니다.");
        }
      } finally {
        running = false;
      }
    };
    void poll();
    const timer = setInterval(() => void poll(), 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [identity, planId, refreshDetail]);
  useEffect(() => {
    const timer = setInterval(() => setObservedTick(Date.now()), 250);
    return () => clearInterval(timer);
  }, []);
  async function run(action: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(success);
      await refreshCatalog();
      await refreshPlans();
      if (planId) await refreshDetail();
    } catch (e) {
      setError(e instanceof Error ? e.message : "결과를 확인하지 못했습니다.");
    } finally {
      setBusy(false);
      setConfirmation(null);
    }
  }
  async function chooseFile(
    file: File | undefined,
    target: (text: string) => void,
  ) {
    if (!file) return;
    if (file.size > 1_048_576) {
      setError("파일 크기는 1 MiB 이하여야 합니다.");
      return;
    }
    const text = await file.text();
    try {
      JSON.parse(text);
      target(text);
    } catch {
      setError("올바른 JSON 파일이 필요합니다.");
    }
  }
  function selectPlan(id: string) {
    context.current = { application, planId: id };
    setDetail(null);
    setObservation(null);
    setApprovalId("");
    setConfirmation(null);
    setPlanId(id);
  }
  function chooseApplication(id: string) {
    context.current = { application: id, planId: "" };
    setApplication(id);
    setPlanId("");
    setDetail(null);
    setObservation(null);
    setConfirmation(null);
    setFields({
      snapshot_id: "",
      source_profile: "",
      target_profile: "",
      manifest: "",
      qualification: "",
      nodes: "",
      rows: "",
    });
  }
  const select = (
    name: keyof typeof fields,
    k: string,
    label: string,
    excludes?: string,
  ) => (
    <label>
      {label}
      <select
        aria-label={label}
        value={fields[name]}
        onChange={(e) => setFields({ ...fields, [name]: e.target.value })}
      >
        <option value="">선택하세요</option>
        {(catalog[k]?.items || [])
          .filter((item) => item.subject_id !== excludes)
          .map((item) => (
            <option key={item.subject_id} value={item.subject_id}>
              {item.subject_id}
            </option>
          ))}
      </select>
    </label>
  );
  const fresh =
    observation && observedTick < Date.parse(observation.expires_at);

  return (
    <div className="app-shell workspace-product">
      <aside className="sidebar">
        <a className="brand" href="/workspace/">
          <span className="brand-mark">
            <Layers3 size={22} />
          </span>
          <strong>
            sova<span>.</span>
          </strong>
        </a>
        <div className="workspace-label">
          <span className="workspace-avatar">S</span>
          <div>
            <b>Migration workspace</b>
            <small>같은 계획으로 검토·실행·확인</small>
          </div>
        </div>
        <p className="nav-caption">WORKSPACE</p>
        <nav aria-label="이전 메뉴">
          {[
            ["plan", "이전 계획", FileCheck2],
            ["register", "환경·앱 등록", Upload],
            ["execute", "승인·실행", ArrowRight],
            ["observe", "워크로드·보안", ShieldCheck],
            ["evidence", "검증 기록", Activity],
          ].map(([value, label, Icon]) => {
            const Glyph = Icon as typeof Activity;
            return (
              <button
                key={String(value)}
                className={tab === value ? "active" : ""}
                onClick={() => setTab(String(value))}
              >
                <Glyph size={18} />
                {String(label)}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-help">
          <ShieldCheck size={20} />
          <b>근거를 확인한 다음 실행</b>
          <p>
            같은 앱·계획의 승인과 원장을 연결합니다. 미확인은 성공으로 바꾸지
            않습니다.
          </p>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            Sova<span>/</span>
            <b>{application || "워크스페이스"}</b>
          </div>
          <div className="topbar-right">
            <span className="workspace-identity">
              {identity
                ? `${identity.subject} · ${identity.roles.join(", ")}`
                : "로그인 필요"}
            </span>
            {identity && (
              <button
                className="icon-button"
                aria-label="로그아웃"
                onClick={() => void logout()}
              >
                <LogOut size={17} />
              </button>
            )}
          </div>
        </header>
        <main>
          <section className="page-heading">
            <div>
              <p className="eyebrow">
                REGISTER · PLAN · APPROVE · EXECUTE · VERIFY
              </p>
              <h1>환경을 바꿔도, 같은 기준으로.</h1>
              <p>등록부터 승인·실행·검증까지 같은 이전 건으로 확인합니다.</p>
            </div>
            {identity && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() =>
                  void run(async () => {}, "현재 서버 기록을 조회했습니다.")
                }
              >
                <RefreshCw size={16} />
                새로고침
              </button>
            )}
          </section>
          {error && (
            <div className="workspace-message error" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="workspace-message" role="status">
              {notice}
            </div>
          )}
          {!identity ? (
            <section className="native-card">
              <h2>{loading ? "인증 확인 중" : "사용자 로그인"}</h2>
              <p>
                인증 서버에서 발급한 본인 권한으로 등록·계획·승인·실행을
                요청합니다. 계획 작성자는 같은 계획을 승인할 수 없습니다.
              </p>
              <button
                className="button primary"
                disabled={loading}
                onClick={() =>
                  void login().catch(() =>
                    setError("로그인을 시작하지 못했습니다."),
                  )
                }
              >
                <LogIn size={17} />
                로그인
              </button>
            </section>
          ) : (
            <>
              <section className="native-card workspace-selection">
                <label>
                  앱
                  <select
                    aria-label="앱"
                    value={application}
                    disabled={busy || confirmation !== null}
                    onChange={(e) => chooseApplication(e.target.value)}
                  >
                    <option value="">등록된 앱 선택</option>
                    {(catalog.service_contract?.items || []).map((item) => (
                      <option key={item.subject_id}>{item.subject_id}</option>
                    ))}
                  </select>
                </label>
                <label>
                  계획
                  <select
                    aria-label="계획"
                    value={planId}
                    disabled={busy || confirmation !== null}
                    onChange={(e) => selectPlan(e.target.value)}
                  >
                    <option value="">계획 선택</option>
                    {plans.items.map((plan) => (
                      <option key={plan.plan_id}>{plan.plan_id}</option>
                    ))}
                  </select>
                </label>
                <div>
                  <small>현재 테넌트</small>
                  <p>{identity.tenant_id}</p>
                </div>
              </section>
              {Object.values(catalog).some(
                (page) => page.next_after !== null,
              ) && (
                <p className="workspace-message">
                  한 페이지에 표시되지 않은 등록이 있습니다. 대상 ID를 기존 조회
                  API로 확인해 등록 범위를 좁히세요. 전체 목록을 확인한 것으로
                  표시하지 않습니다.
                </p>
              )}
              {tab === "register" && (
                <div className="workspace-two">
                  <section className="native-card">
                    <h2>환경·서비스 등록</h2>
                    <p>
                      등록은 실행 권한이 아닙니다. 서명 검증과 방향별 자격이
                      준비돼야 계획을 만들 수 있습니다.
                    </p>
                    <label>
                      등록 종류
                      <select
                        value={kind}
                        onChange={(e) => setKind(e.target.value)}
                      >
                        {kinds.map((k) => (
                          <option value={k} key={k}>
                            {kindNames[k]}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      등록 ID
                      <input
                        aria-label="등록 ID"
                        value={subject}
                        onChange={(e) => setSubject(e.target.value)}
                      />
                    </label>
                    <label>
                      읽은 revision
                      <input
                        type="number"
                        min="0"
                        value={revision}
                        onChange={(e) => setRevision(Number(e.target.value))}
                      />
                    </label>
                    <label>
                      등록 JSON 파일
                      <input
                        type="file"
                        accept="application/json,.json"
                        onChange={(e) =>
                          void chooseFile(e.target.files?.[0], setDocument)
                        }
                      />
                    </label>
                    <textarea
                      aria-label="등록 JSON"
                      value={document}
                      onChange={(e) => setDocument(e.target.value)}
                      maxLength={1_048_576}
                      rows={10}
                    />
                    <button
                      className="button primary"
                      disabled={
                        busy || !has("registrar") || !subject || !document
                      }
                      onClick={() =>
                        void run(
                          () =>
                            api(
                              `/registrations/${segment(kind)}/${segment(subject)}`,
                              {
                                document: JSON.parse(document),
                                expected_revision: revision,
                              },
                              "PUT",
                            ),
                          "등록을 저장했습니다. 서명·자격 확인은 별도입니다.",
                        )
                      }
                    >
                      등록 저장
                    </button>
                  </section>
                  <section className="native-card">
                    <h2>독립 검증자의 서명 등록</h2>
                    <p>
                      기존에 신뢰 등록된 검증자의 서명 봉투를 제출합니다. 화면이
                      PASS나 서명을 만들지 않습니다.
                    </p>
                    <textarea
                      aria-label="서명 JSON"
                      value={envelope}
                      onChange={(e) => setEnvelope(e.target.value)}
                      rows={12}
                      maxLength={1_048_576}
                    />
                    <button
                      className="button secondary"
                      disabled={busy || !has("attestor") || !envelope}
                      onClick={() =>
                        void run(
                          () => api("/attestations", JSON.parse(envelope)),
                          "서명 봉투가 서버 검증을 통과해 저장됐습니다.",
                        )
                      }
                    >
                      서명 제출
                    </button>
                  </section>
                </div>
              )}
              {tab === "plan" && (
                <section className="native-card">
                  <h2>이전 계획 생성</h2>
                  <p>
                    등록된 입력으로 기존 컴파일러가 순서·검증 의무·복구 경계를
                    고정합니다.
                  </p>
                  <div className="workspace-form">
                    {select(
                      "snapshot_id",
                      "dependency_snapshot",
                      "의존성 조사",
                    )}
                    {select(
                      "source_profile",
                      "environment_profile",
                      "출발 환경",
                    )}
                    {select(
                      "target_profile",
                      "environment_profile",
                      "목적지 환경",
                      fields.source_profile,
                    )}
                    {select("manifest", "capability_manifest", "이전 어댑터")}
                    {select(
                      "qualification",
                      "strategy_qualification",
                      "방향별 자격",
                    )}
                    <label>
                      이전 구성 요소 ID (쉼표 구분)
                      <input
                        aria-label="이전 구성 요소"
                        value={fields.nodes}
                        onChange={(e) =>
                          setFields({ ...fields, nodes: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      계획의 행 수 상한
                      <input
                        aria-label="행 수 상한"
                        type="number"
                        min="1"
                        value={fields.rows}
                        onChange={(e) =>
                          setFields({ ...fields, rows: e.target.value })
                        }
                      />
                    </label>
                  </div>
                  <button
                    className="button primary"
                    disabled={
                      busy ||
                      !has("planner") ||
                      !application ||
                      Object.values(fields).some((v) => !v)
                    }
                    onClick={() =>
                      void run(async () => {
                        const result = await api<{
                          status: string;
                          plan_id?: string;
                          blockers?: unknown[];
                        }>("/plans", {
                          service_id: application,
                          ...fields,
                          rows: Number(fields.rows),
                          nodes: fields.nodes
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean),
                        });
                        if (result.status !== "COMPILED" || !result.plan_id) {
                          setNotice(JSON.stringify(result.blockers || result));
                          throw new Error(
                            "계획 생성이 보류됐습니다. 등록·자격·의존성 근거를 확인하세요.",
                          );
                        }
                        selectPlan(result.plan_id);
                      }, "계획을 저장했습니다. 독립 승인과 실행 전 재검증이 필요합니다.")
                    }
                  >
                    계획 만들기
                  </button>
                </section>
              )}
              {(tab === "plan" || tab === "execute") && detail && (
                <section className="native-card">
                  <h2>동결 계획 검토</h2>
                  <dl className="workspace-binding">
                    <dt>계획 ID</dt>
                    <dd>{detail.plan_id}</dd>
                    <dt>계획 digest</dt>
                    <dd>{detail.plan_digest}</dd>
                    <dt>작성자</dt>
                    <dd>{detail.plan.created_by}</dd>
                    <dt>유효 기한</dt>
                    <dd>{detail.plan.valid_until}</dd>
                    <dt>이전 단위</dt>
                    <dd>{detail.compiled.unit.members.join(", ")}</dd>
                  </dl>
                  <ol className="workspace-steps">
                    {detail.plan.steps.map((step) => (
                      <li key={step.step_id}>
                        <b>{step.action}</b>
                        <span>
                          {step.scope.environment_id} / {step.scope.resource_id}
                        </span>
                      </li>
                    ))}
                  </ol>
                  <p>
                    검증 의무 {detail.plan.obligations.length}개 · 계획 조회는
                    실행 허가가 아닙니다.
                  </p>
                </section>
              )}
              {tab === "execute" && detail && (
                <section className="native-card">
                  <h2>독립 승인·실행 접수</h2>
                  <p>
                    현재 로그인한 주체의 권한을 서버가 다시 검사합니다. 실행
                    시점에는 입력·보안 보류·현재 승인도 다시 확인합니다.
                  </p>
                  <button
                    className="button primary"
                    disabled={
                      busy ||
                      !has("approver") ||
                      identity.subject === detail.plan.created_by
                    }
                    onClick={() => {
                      const id = "approval-" + crypto.randomUUID();
                      setConfirmation({
                        title: "표시한 동결 계획을 승인할까요?",
                        application,
                        planId,
                        digest: detail.plan_digest,
                        run: () =>
                          api(`${base}/plans/${segment(planId)}/approvals`, {
                            approval_id: id,
                            valid_minutes: 10,
                          }),
                      });
                    }}
                  >
                    계획 승인
                  </button>
                  {identity.subject === detail.plan.created_by && (
                    <p className="workspace-muted">
                      계획 작성자는 이 계획을 승인할 수 없습니다. 다른 승인자가
                      검토해야 합니다.
                    </p>
                  )}
                  <label>
                    저장된 승인
                    <select
                      aria-label="저장된 승인"
                      value={approvalId}
                      onChange={(e) => setApprovalId(e.target.value)}
                    >
                      <option value="">승인 선택</option>
                      {detail.approvals
                        .filter(
                          (a) =>
                            !a.revoked &&
                            Date.parse(a.expires_at) > observedTick,
                        )
                        .map((a) => (
                          <option key={a.approval_id} value={a.approval_id}>
                            {a.approved_by} · {a.approval_id}
                          </option>
                        ))}
                    </select>
                  </label>
                  <button
                    className="button secondary"
                    disabled={
                      busy ||
                      !has("operator") ||
                      !approvalId ||
                      observation !== null
                    }
                    onClick={() =>
                      setConfirmation({
                        title: "승인한 계획의 실제 이전을 접수할까요?",
                        application,
                        planId,
                        digest: detail.plan_digest,
                        run: () =>
                          api(`${base}/admissions`, {
                            approval_id: approvalId,
                          }),
                      })
                    }
                  >
                    이전 실행 접수
                  </button>
                  <p>
                    접수는 완료가 아닙니다. 워커의 실제 효과·검증 결과는
                    워크로드·보안 메뉴에서 확인합니다.
                  </p>
                </section>
              )}
              {tab === "observe" &&
                (fresh ? (
                  <NativePlanView data={observation} />
                ) : (
                  <section className="native-card">
                    <h2>현재 실행 관측 없음</h2>
                    <p>
                      계획을 선택하고 실제 실행을 접수한 뒤 관측합니다. 응답
                      실패·만료를 정상으로 표시하지 않습니다.
                    </p>
                  </section>
                ))}
              {tab === "evidence" && detail && (
                <section className="native-card">
                  <h2>검증·복구 경계</h2>
                  <p>
                    현재 계획의 서버 기록입니다. 서명 증거는 기존 API에서 원문
                    관측과 연결해 확인합니다.
                  </p>
                  <pre>
                    {JSON.stringify(
                      {
                        plan_digest: detail.plan_digest,
                        compiled_digest: detail.compiled_digest,
                        obligations: detail.plan.obligations,
                        current: fresh ? observation : "unavailable",
                      },
                      null,
                      2,
                    )}
                  </pre>
                </section>
              )}
            </>
          )}
        </main>
      </div>
      {confirmation && (
        <div className="modal-backdrop">
          <section
            className="confirmation-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="workspace-confirm"
          >
            <h2 id="workspace-confirm">{confirmation.title}</h2>
            <p>
              앱 {confirmation.application} · 계획 {confirmation.planId}
            </p>
            <code>{confirmation.digest}</code>
            <p>서버의 역할·계획 결속·유효기간 검사를 통과해야 접수됩니다.</p>
            <div className="workspace-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => setConfirmation(null)}
              >
                취소
              </button>
              <button
                className="button primary"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    if (
                      context.current.application !==
                        confirmation.application ||
                      context.current.planId !== confirmation.planId ||
                      detail?.plan_digest !== confirmation.digest
                    )
                      throw new Error(
                        "검토한 계획이 바뀌었습니다. 다시 확인하세요.",
                      );
                    return confirmation.run();
                  }, "요청이 서버에 저장됐습니다. 현재 원장으로 진행 상황을 확인하세요.")
                }
              >
                확인하고 요청
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
