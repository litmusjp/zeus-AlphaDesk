import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DeskOverview, opportunitySummary, approvalSummary, positionsOrdersSummary, profitAndLoss } from "./desk-overview";

const deskFetch = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

describe("DeskOverview summaries", () => {
  beforeEach(() => deskFetch.mockReset());

  it("calculates broker-confirmed P/L", () => {
    expect(profitAndLoss({ equity: "101250", last_equity: "100000" })).toEqual({ value: "$1,250.00", tone: "good" });
    expect(profitAndLoss(null).value).toBe("—");
  });

  it("uses the newest unique opportunity state per symbol", () => {
    expect(opportunitySummary([
      { symbol: "AAPL", disposition: "NO_TRADE", observed_at: "2026-01-01T00:00:00Z" },
      { symbol: "AAPL", disposition: "TRADE", observed_at: "2026-01-02T00:00:00Z" },
      { symbol: "MSFT", disposition: "RISK_REJECTED" },
    ], 3)).toBe("3 symbols tracked · 1 trade · 1 risk rejected · 1 unavailable");
  });

  it("breaks approvals into active, filled, and failed", () => {
    expect(approvalSummary([{ state: "APPROVED_FOR_SESSION" }, { state: "REVALIDATING" }, { state: "READY_TO_SUBMIT" }, { state: "FILLED" }, { state: "EXPIRED" }, { state: "BROKER_REJECTED" }, { state: "CONDITION_FAILED" }])).toBe("3 active · 1 filled · 3 failed");
  });

  it("counts non-zero positions and working orders", () => {
    expect(positionsOrdersSummary([{ quantity: "0" }, { quantity: "-2" }, { quantity: 1 }], [{ status: "new" }, { status: "PARTIALLY_FILLED" }, { status: "filled" }])).toBe("2 active positions · 2 working orders");
  });

  it("renders summaries and preserves each unavailable subcount", async () => {
    deskFetch.mockImplementation((path: string) => {
      if (path === "/desk/opportunities") return Promise.resolve([{ symbol: "AAPL", disposition: "NO_TRADE" }]);
      if (path === "/desk/approvals") return Promise.resolve([{ state: "FILLED" }, { state: "EXPIRED" }]);
      if (path === "/desk/broker/positions") return Promise.reject(new Error("unavailable"));
      if (path === "/desk/broker/orders") return Promise.resolve([{ status: "accepted" }]);
      return Promise.resolve({
        "/identity/me": { email: "operator@example.test", is_admin: false, workspace_id: "workspace-1", workspace_status: "ACTIVE" },
        "/desk/workspace": { status: "ACTIVE", scanner_enabled: true, watchlist_count: 2 },
        "/desk/broker/account": { equity: "101250", last_equity: "100000" },
        "/desk/broker/status": { state: "RECONCILED", stream_connected: true, last_reconciled_at: null },
      }[path]);
    });
    render(<DeskOverview />);
    expect(await screen.findByText("2 symbols tracked · 1 no trade · 1 unavailable · Open scanner")).toBeInTheDocument();
    expect(screen.getByText("0 active · 1 filled · 1 failed · Open approvals")).toBeInTheDocument();
    expect(screen.getByText("— active positions · 1 working orders · Open positions")).toBeInTheDocument();
  });

  it("shows a neutral unavailable state when an action endpoint fails", async () => {
    deskFetch.mockImplementation((path: string) => {
      if (["/desk/opportunities", "/desk/approvals", "/desk/broker/positions", "/desk/broker/orders"].includes(path)) return Promise.reject(new Error("not available"));
      return Promise.resolve({ "/identity/me": { email: "operator@example.test", is_admin: false, workspace_id: "workspace-1" }, "/desk/workspace": { status: "ACTIVE", scanner_enabled: false, watchlist_count: 0 }, "/desk/broker/account": null, "/desk/broker/status": { state: "UNKNOWN", stream_connected: false } }[path]);
    });
    render(<DeskOverview />);
    expect(await screen.findByText("— unavailable · Open scanner")).toBeInTheDocument();
    expect(screen.getByText("— unavailable · Open approvals")).toBeInTheDocument();
    expect(screen.getByText("— active positions · — working orders · Open positions")).toBeInTheDocument();
  });
});
