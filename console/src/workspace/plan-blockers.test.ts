import { describe, expect, it } from "vitest";
import { blockerDescription, checkedBlockers } from "./PlanBlockers";

describe("native refusal reasons remain refusals", () => {
  it.each([
    undefined,
    [],
    [{ stage: "C2", code: "" }],
    [{ stage: "C2", code: "<script>" }],
    [{ stage: "PASS", code: "WRITER_INVENTORY_INCOMPLETE" }],
    Array.from({ length: 101 }, () => ({ stage: "C2", code: "UNKNOWN" })),
    [{ stage: "C2", code: "UNKNOWN", detail: "x".repeat(16_384) }],
  ])("rejects malformed, unbounded or fabricated pass data", (value) => {
    expect(() => checkedBlockers(value)).toThrow();
  });

  it("keeps writer, controller and coverage decisions separate", () => {
    const codes = [
      "WRITER_INVENTORY_INCOMPLETE",
      "CONTROL_INVENTORY_INCOMPLETE",
      "DISCOVERY_COVERAGE_INCOMPLETE",
    ];
    const rows = checkedBlockers(codes.map((code) => ({ stage: "C2", code })));
    expect(rows.map((row) => row.code)).toEqual(codes);
    expect(
      new Set(rows.map((row) => blockerDescription(row.code).title)).size,
    ).toBe(3);
  });

  it("retains unknown server codes and decision details without treating them as success", () => {
    const [row] = checkedBlockers([
      {
        stage: "C12",
        code: "FUTURE_REQUIRED_PROOF",
        additional_members: ["batch"],
      },
    ]);
    expect(JSON.parse(row.details).additional_members).toEqual(["batch"]);
    expect(blockerDescription(row.code).action).toContain(
      "통과로 바꾸지 않습니다",
    );
  });
});
