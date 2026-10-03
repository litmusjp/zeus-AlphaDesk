import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MarketScanner } from "./market-scanner";

const deskFetch = vi.fn();
vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

describe("MarketScanner initial reads", () => {
  it("shows saved scan history when the market clock read fails", async () => {
    deskFetch.mockReset();
    deskFetch.mockImplementation((path: string) => {
      if (path === "/desk/watchlist") return Promise.resolve(["AAPL"]);
      if (path === "/desk/workspace") return Promise.resolve({ scanner_enabled: true, watchlist_count: 1, status: "ACTIVE" });
      if (path === "/desk/scanner/runs") return Promise.resolve([{ scan_run_id: "run-1", trigger: "MANUAL", source: "ALPACA_REAL", started_at: "2026-09-25T15:00:00Z", completed_at: "2026-09-25T15:01:00Z", attempted: 1, completed: 1, failed: 0 }]);
      if (path === "/desk/market-clock") return Promise.reject(new Error("Clock request failed"));
      if (path === "/desk/scanner/runs/run-1") return Promise.resolve([{ opportunity_id: "opportunity-1", scan_run_id: "run-1", symbol: "AAPL", disposition: "NO_TRADE", source: "ALPACA_REAL", observed_at: "2026-09-25T15:00:00Z", expires_at: "2026-09-25T15:02:00Z", signal: { score: 42 }, candidate: null, order_intent: null, option_diagnostics: null, reason_codes: [] }]);
      throw new Error(`Unexpected path: ${path}`);
    });
    render(<MarketScanner />);
    expect(await screen.findByText("Clock request failed")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Review/ })).toHaveAttribute("href", "/desk/opportunities/opportunity-1");
    expect(screen.getByRole("checkbox", { name: /5-minute market-hours scan/ })).toBeChecked();
  });
});
