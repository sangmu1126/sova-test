export const nextSteps: Record<string, string> = {
  REVIEW_SECURITY_HOLD: "보안 보류의 근거와 해제 조건을 검토",
  VERIFY_REMOTE_SETTLEMENT: "이전 실행과 원격 요청이 모두 멈췄는지 확인",
  READBACK_EXISTING_COMMAND: "기존 명령의 실제 결과를 다시 조회",
  REVIEW_RECOVERY_PLAN: "확인한 근거로 복구 계획을 검토",
};
export const reasons: Record<string, string> = {
  EFFECT_RESULT_UNKNOWN: "효과 결과 미확인",
  RECOVERY_PENDING: "읽기 전용 복구 확인 중",
  OWNER_EXPIRED_UNSETTLED: "실행권 만료 · 미확정 효과 남음",
  TERMINAL_WITH_PENDING_EFFECTS: "종료 상태와 효과 원장 불일치",
  APPLICATION_PENDING_EFFECTS: "같은 앱의 다른 작업에 미확정 효과 남음",
  SECURITY_HOLD: "보안 보류",
  AUTHORITY_HOLD: "권한 이전 보류",
  OBSERVATION_UNAVAILABLE: "원장 관측 불가",
};
const states = [
  "SOURCE_ACTIVE",
  "TARGET_PREPARED",
  "SYNCING",
  "CUTOVER_READY",
  "QUIESCING",
  "SOURCE_FENCED",
  "FINAL_STATE_VERIFIED",
  "TARGET_ACTIVATION_INTENT",
  "TARGET_ACTIVE_OBSERVED",
  "ROUTE_CONFIRMED",
  "STABILIZING",
  "HANDOFF_COMPLETE",
  "SAFE_HOLD",
  "ABORTING_PRE_ACTIVATION",
  "RESTORING_SOURCE",
  "FAILBACK_REQUIRED",
  "ABORTED",
];
export type NativeStatus = {
  observation_version: "handoff-recovery-status-v2";
  scope: {
    tenant_id: string;
    application_id: string;
    unit_digest: string;
    plan_digest: string;
    operation_id: string | null;
  };
  state: string;
  epoch: number;
  next_operation: number;
  owner_environment: string;
  activation_possible: boolean;
  observed_at: string;
  expires_at: string;
  recovery: {
    status: "required" | "clear" | "unavailable";
    required: boolean | null;
    reason: string | null;
    security_holds: {
      active_count: number;
      hold_ids: string[];
      operation_hold: boolean | null;
    };
    pending_effects: {
      fingerprint: string;
      action: string;
      status: string;
      revision: number;
    }[];
    next_steps: string[];
    automatic_replay_allowed: false;
    authorizes_execution: false;
  };
};
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const id = (v: unknown): v is string =>
  typeof v === "string" &&
  v.length > 0 &&
  v.length <= 256 &&
  v.trim() === v &&
  !/[\u0000-\u001f\u007f]/.test(v);
const digest = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const stamp = (v: unknown): number =>
  typeof v === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(v)
    ? Date.parse(v)
    : NaN;

export function nativeStatus(value: unknown, now = Date.now()): NativeStatus {
  const fail = () => {
    throw new Error("현재 계획의 원장 관측을 확인할 수 없습니다.");
  };
  if (
    !object(value) ||
    value.observation_version !== "handoff-recovery-status-v2" ||
    !object(value.scope) ||
    !object(value.recovery)
  )
    return fail();
  const s = value.scope,
    r = value.recovery;
  const observed = stamp(value.observed_at),
    expires = stamp(value.expires_at);
  if (
    !id(s.tenant_id) ||
    !id(s.application_id) ||
    !digest(s.unit_digest) ||
    !digest(s.plan_digest) ||
    !(s.operation_id === null || id(s.operation_id)) ||
    !states.includes(String(value.state)) ||
    !id(value.owner_environment) ||
    !Number.isSafeInteger(value.epoch) ||
    Number(value.epoch) < 1 ||
    !Number.isSafeInteger(value.next_operation) ||
    Number(value.next_operation) < 0 ||
    typeof value.activation_possible !== "boolean" ||
    !Number.isFinite(observed) ||
    !Number.isFinite(expires) ||
    expires <= observed ||
    expires - observed > 5000 ||
    observed > now + 1000 ||
    now >= expires ||
    r.automatic_replay_allowed !== false ||
    r.authorizes_execution !== false ||
    !Array.isArray(r.pending_effects) ||
    r.pending_effects.length > 256 ||
    !Array.isArray(r.next_steps)
  )
    return fail();
  const holds = r.security_holds;
  if (
    !object(holds) ||
    !Array.isArray(holds.hold_ids) ||
    holds.hold_ids.length > 256 ||
    !holds.hold_ids.every(id) ||
    new Set(holds.hold_ids).size !== holds.hold_ids.length ||
    !Number.isSafeInteger(holds.active_count) ||
    holds.active_count !== holds.hold_ids.length ||
    (holds.operation_hold !== null &&
      typeof holds.operation_hold !== "boolean") ||
    (r.status !== "unavailable" && holds.operation_hold === null)
  )
    return fail();
  const securityBlocked = holds.hold_ids.length > 0 || holds.operation_hold;
  if (!(
    (r.status === "clear" && r.required === false && r.reason === null) ||
    (r.status === "required" &&
      r.required === true &&
      typeof r.reason === "string" &&
      r.reason !== "OBSERVATION_UNAVAILABLE" &&
      Object.hasOwn(reasons, r.reason)) ||
    (r.status === "unavailable" &&
      r.required === null &&
      r.reason === "OBSERVATION_UNAVAILABLE")
  ))
    return fail();
  if (
    (securityBlocked && r.status === "clear") ||
    (r.reason === "SECURITY_HOLD" && !securityBlocked)
  )
    return fail();
  const expected = securityBlocked ? ["REVIEW_SECURITY_HOLD"] : [];
  if (r.required === true && r.reason !== "SECURITY_HOLD")
    expected.push(
      "VERIFY_REMOTE_SETTLEMENT",
      "READBACK_EXISTING_COMMAND",
      "REVIEW_RECOVERY_PLAN",
    );
  if (JSON.stringify(r.next_steps) !== JSON.stringify(expected)) return fail();
  const seen = new Set<string>();
  for (const e of r.pending_effects) {
    if (
      !object(e) ||
      !digest(e.fingerprint) ||
      seen.has(e.fingerprint) ||
      !id(e.action) ||
      !["prepared", "in_flight", "unknown"].includes(String(e.status)) ||
      !Number.isSafeInteger(e.revision) ||
      Number(e.revision) < 1 ||
      (e.status === "unknown" && r.status !== "required")
    )
      return fail();
    seen.add(e.fingerprint);
  }
  return value as NativeStatus;
}
