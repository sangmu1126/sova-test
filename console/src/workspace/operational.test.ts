import { describe, expect, it } from "vitest";
import { checkedObservations } from "./OperationalPanel";
const make = () => ({
  version: 1,
  tenant_id: "t",
  application_id: "a",
  observational_only: true,
  authorizes_execution: false,
  health: {
    state: "fresh",
    observed_at: "2026-11-20T00:00:00Z",
    valid_until: "2026-11-20T00:05:00Z",
    record_sha256: "a".repeat(64),
    samples: [
      {
        side: "source",
        status: 200,
        duration_ms: 20,
        verified_application_https: true,
      },
    ],
  },
  cost: { state: "unavailable", available: false, amount: null },
});
describe("operational measurements never authorize execution", () => {
  it("keeps unknown billing distinct from zero", () =>
    expect(checkedObservations(make(), "t", "a").cost.amount).toBeNull());
  it.each([
    "tenant_id",
    "application_id",
    "authorizes_execution",
    "observational_only",
  ])("rejects mismatched %s", (key) => {
    const row = make();
    Object.assign(row, { [key]: "other" });
    expect(() => checkedObservations(row, "t", "a")).toThrow();
  });
  it("refuses an estimated zero", () => {
    const row = make();
    Object.assign(row.cost, {
      available: true,
      amount: "0",
      currency: "USD",
      estimated: true,
    });
    expect(() => checkedObservations(row, "t", "a")).toThrow();
  });
  it("rejects invalid finite latency", () => {
    const row = make();
    row.health.samples[0].duration_ms = Infinity;
    expect(() => checkedObservations(row, "t", "a")).toThrow();
  });
});
