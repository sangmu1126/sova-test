import { describe, expect, it } from "vitest";
import { checkedHistory } from "./ChangeHistory";
const make = () => ({
  version: 1,
  tenant_id: "t",
  application_id: "a",
  observational_only: true,
  authorizes_execution: false,
  coverage: "UNKNOWN",
  next_after: null as string | null,
  items: [
    {
      event_id: "e",
      kind: "schema",
      environment_id: "source",
      subject: "db",
      observed_at: "2026-11-20T00:00:00Z",
      recorded_at: "2026-11-20T00:00:01Z",
      collector_id: "c",
    },
  ],
});
describe("change history has no execution authority", () => {
  it("keeps an empty history unknown", () => {
    const h = make();
    h.items = [];
    expect(checkedHistory(h, "t", "a").coverage).toBe("UNKNOWN");
  });
  it("preserves stored observations without inventing a deadline", () => {
    expect(checkedHistory(make(), "t", "a").items[0].kind).toBe("schema");
  });
  it.each([
    "tenant_id",
    "application_id",
    "coverage",
    "authorizes_execution",
    "observational_only",
  ])("rejects %s mismatch", (key) => {
    expect(() =>
      checkedHistory({ ...make(), [key]: "wrong" }, "t", "a"),
    ).toThrow();
  });
  it("rejects duplicates, oversized pages and invalid row times", () => {
    const h = make();
    h.items.push(h.items[0]);
    expect(() => checkedHistory(h, "t", "a")).toThrow();
    h.items = Array(21).fill(h.items[0]);
    expect(() => checkedHistory(h, "t", "a")).toThrow();
    h.items = [make().items[0]];
    h.items[0].recorded_at = "2026-11-20T00:00:00";
    expect(() => checkedHistory(h, "t", "a")).toThrow();
  });
  it("rejects a next page for an incomplete page", () => {
    const h = make();
    h.next_after = "cursor";
    expect(() => checkedHistory(h, "t", "a")).toThrow();
  });
});
