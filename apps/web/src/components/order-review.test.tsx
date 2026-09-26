import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OrderReview } from "./order-review";

const deskFetch = vi.fn();
vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

const opportunity = {
  opportunity_id: "opportunity-1", symbol: "AAPL", disposition: "TRADE", source: "ALPACA_REAL",
  observed_at: "2026-09-25T23:00:00Z", expires_at: "2027-09-26T00:00:00Z", signal: { score: 80 },
  candidate: { structure: { structure_type: "DEBIT_VERTICAL", quantity: 1, max_loss: "220", max_profit: "100", net_premium_per_share: "2.20", break_evens: [], legs: [] } },
  risk_decision: { decision: "APPROVE", checks: [] }, order_intent: { client_order_id: "client-order-1", quantity: 1, limit_price: "2.20" },
  option_diagnostics: null, reason_codes: [],
};

describe("OrderReview exit plan", () => {
  it("loads the exchange-session recommendation and posts it as an instant", async () => {
    deskFetch.mockReset();
    deskFetch.mockResolvedValueOnce(opportunity).mockResolvedValueOnce([]).mockResolvedValueOnce({ trading_environment: "PAPER" }).mockResolvedValueOnce({ available: true, session_date: "2026-09-28", session_open: "2026-09-28T13:30:00Z", session_close: "2026-09-28T20:00:00Z", recommended_exit_at: "2026-09-28T19:55:00Z", timezone: "America/New_York", unavailable_reason: null }).mockResolvedValueOnce({ approval_id: "approval-1", opportunity_id: "opportunity-1", state: "APPROVED_FOR_SESSION", exit_plan: null });
    render(<OrderReview id="opportunity-1" />);
    const expiry = await screen.findByLabelText("Time-based exit");
    expect(expiry).toHaveValue("2026-09-28T15:55");
    fireEvent.change(screen.getByLabelText("Maximum loss exit"), { target: { value: "220" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve for next U.S. session" }));
    await waitFor(() => expect(deskFetch.mock.calls.some(([, request]) => (request as RequestInit | undefined)?.method === "POST")).toBe(true));
    const request = deskFetch.mock.calls.find(([, request]) => (request as RequestInit | undefined)?.method === "POST")?.[1] as RequestInit;
    const body = JSON.parse(String(request.body));
    expect(body.exit_plan.expires_at).toBe("2026-09-28T19:55:00.000Z");
    expect(body.exit_plan.expires_at).not.toBe(opportunity.expires_at);
  });
});
