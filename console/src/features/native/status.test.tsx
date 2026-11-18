import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { NativePlanView } from "./NativePlanPanel";
import { nativeStatus, nextSteps } from "./status";

const now = Date.parse("2026-11-19T10:00:00Z");
function observation() {
  return {
    observation_version: "handoff-recovery-status-v1",
    scope: {
      tenant_id: "tenant-a",
      application_id: "orders",
      unit_digest: "a".repeat(64),
      plan_digest: "b".repeat(64),
      operation_id: "handoff-a",
    },
    state: "SOURCE_FENCED",
    epoch: 2,
    next_operation: 3,
    owner_environment: "source",
    activation_possible: false,
    observed_at: "2026-11-19T10:00:00Z",
    expires_at: "2026-11-19T10:00:05Z",
    recovery: {
      status: "required",
      required: true,
      reason: "EFFECT_RESULT_UNKNOWN",
      pending_effects: [
        {
          fingerprint: "c".repeat(64),
          action: "copy_content",
          status: "unknown",
          revision: 2,
        },
      ],
      next_steps: Object.keys(nextSteps),
      automatic_replay_allowed: false,
      authorizes_execution: false,
    },
  };
}
describe("native recovery observation", () => {
  it("shows the hold separately from the witnessed authority, without action buttons", () => {
    const view = renderToStaticMarkup(
      <NativePlanView data={nativeStatus(observation(), now)} />,
    );
    expect(view).toContain("SOURCE_FENCED");
    expect(view).toContain("실행 보류 · 운영 검토 필요");
    expect(view).toContain("copy_content");
    expect(view).toContain("unknown");
    expect(view).toContain("이전 실행과 원격 요청이 모두 멈췄는지 확인");
    expect(view).not.toContain("SAFE_HOLD");
    expect(view).not.toContain("<button");
    expect(view).not.toContain("이전 완료");
  });
  it.each([5000, 5001, 60000])(
    "rejects an expired observation at %s ms",
    (offset) => {
      expect(() => nativeStatus(observation(), now + offset)).toThrow();
    },
  );
  it("rejects future timestamps and extended observation lifetimes", () => {
    expect(() => nativeStatus(observation(), now - 1001)).toThrow();
    const value = observation();
    value.expires_at = "2026-11-19T10:00:06Z";
    expect(() => nativeStatus(value, now)).toThrow();
  });
  it("never accepts UNKNOWN as a clear observation", () => {
    const value = observation();
    Object.assign(value.recovery, {
      status: "clear",
      required: false,
      reason: null,
      next_steps: [],
    });
    expect(() => nativeStatus(value, now)).toThrow();
  });
  it("does not present an unavailable list as a verified empty list", () => {
    const value = observation();
    Object.assign(value.recovery, {
      status: "unavailable",
      required: null,
      reason: "OBSERVATION_UNAVAILABLE",
      pending_effects: [],
      next_steps: [],
    });
    const view = renderToStaticMarkup(
      <NativePlanView data={nativeStatus(value, now)} />,
    );
    expect(view).toContain("0건 검증으로 해석하지 않습니다");
    expect(view).not.toContain("현재 관측 범위에 미확정 효과가 없습니다");
  });
  it("rejects duplicate effects, execution flags and unregistered next actions", () => {
    const duplicate = observation();
    duplicate.recovery.pending_effects.push({
      ...duplicate.recovery.pending_effects[0],
    });
    const permit = observation();
    permit.recovery.authorizes_execution = true;
    const replay = observation();
    replay.recovery.automatic_replay_allowed = true;
    const action = observation();
    action.recovery.next_steps = ["RETRY_COPY"];
    for (const value of [duplicate, permit, replay, action])
      expect(() => nativeStatus(value, now)).toThrow();
  });
  it("escapes observed labels instead of executing markup", () => {
    const value = observation();
    value.owner_environment = '<img src=x onerror="bad()">';
    const view = renderToStaticMarkup(
      <NativePlanView data={nativeStatus(value, now)} />,
    );
    expect(view).toContain("&lt;img");
    expect(view).not.toContain("<img");
  });
  it("requires the native version rather than accepting the old stateless envelope", () => {
    expect(() =>
      nativeStatus({ schema: "eks-rke2-stateless-v1", state: "ready" }, now),
    ).toThrow();
    expect(() => nativeStatus({ state: null }, now)).toThrow();
  });
});
