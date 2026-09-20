import { describe, expect, it } from "vitest";
import { recommendPositionAction } from "./position-recommendation";

describe("recommendPositionAction", () => {
  it("fails closed to HOLD when fresh Candidate Assessment evidence is missing", () => {
    const result = recommendPositionAction({
      unrealized_pl: "12.50",
      current_price: "1.25",
    });

    expect(result.label).toBe("HOLD");
    expect(result.rationale).toContain("Fresh Candidate Assessment evidence is unavailable");
    expect(result.rationale).toContain("BUY MORE");
  });

  it.each([
    { unrealized_pl: "not-a-number", current_price: "1.25" },
    { unrealized_pl: "12.50", current_price: "Infinity" },
    { unrealized_pl: "12.50", current_price: null },
  ])("fails closed to HOLD when P/L or price is not finite: %o", (position) => {
    const result = recommendPositionAction(position);

    expect(result.label).toBe("HOLD");
    expect(result.rationale).toContain("Position data is incomplete or invalid");
  });
});
