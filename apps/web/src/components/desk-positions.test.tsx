import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeskPositions } from "./desk-positions";

const deskFetch = vi.fn();
vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

describe("DeskPositions broker reconciliation display", () => {
  beforeEach(() => deskFetch.mockReset());
  afterEach(() => vi.restoreAllMocks());

  const projections = (status: unknown): Record<string, unknown> => ({
    "/desk/broker/account": null,
    "/desk/broker/positions": [{ asset_id: "asset-1", symbol: "AAPL", side: "long", quantity: "1", market_value: "10", unrealized_pl: "0" }],
    "/desk/broker/orders": [],
    "/desk/broker/status": status,
  });

  it("shows authoritative unknown and divergent status without freshness or parity claims", async () => {
    deskFetch.mockImplementation((path: string) => Promise.resolve(projections({
        state: "UNKNOWN",
        last_reconciled_at: "2026-09-28T13:31:10.274585Z",
        last_stream_event_at: "2026-10-16T23:37:55.542272Z",
        stream_connected: true,
        generation: 29716,
        divergence_count: 3,
        failure_reason: "broker_evidence_invalid",
      })[path]));

    render(<DeskPositions />);

    expect(await screen.findByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.getAllByText(/broker_evidence_invalid/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/3 divergences/)).toHaveLength(2);
    expect(screen.getAllByText(/2026-09-28T13:31:10.274585Z/)).toHaveLength(2);
    expect(screen.getByText("BROKER UNKNOWN")).toHaveClass("warn");
    expect(screen.queryByText(/Freshness verified|ALL EXPOSURE RECONCILED|Continuous Broker Parity/i)).not.toBeInTheDocument();
  });

  it("shows a positive reconciliation only for clean authoritative status", async () => {
    deskFetch.mockImplementation((path: string) => Promise.resolve(projections({
      state: "RECONCILED",
      last_reconciled_at: "2026-10-18T10:00:00Z",
      last_stream_event_at: "2026-10-18T10:00:00Z",
      stream_connected: true,
      generation: 12,
      divergence_count: 0,
      failure_reason: null,
    })[path]));

    render(<DeskPositions />);

    expect(await screen.findByText("EXPOSURE RECONCILED")).toHaveClass("good");
    expect(screen.getAllByText("RECONCILED")).toHaveLength(2);
  });

  it("does not label exposure reconciled when the API reports divergences", async () => {
    deskFetch.mockImplementation((path: string) => Promise.resolve(projections({
      state: "RECONCILED",
      last_reconciled_at: "2026-10-18T10:00:00Z",
      last_stream_event_at: null,
      stream_connected: true,
      generation: 13,
      divergence_count: 3,
      failure_reason: "broker_evidence_invalid",
    })[path]));

    render(<DeskPositions />);

    expect(await screen.findByText("RECONCILIATION CHECK REQUIRED")).toHaveClass("warn");
    expect(screen.getByText("BROKER CHECK REQUIRED")).toHaveClass("warn");
    expect(screen.getAllByText(/API state: RECONCILED/).length).toBeGreaterThan(0);
    expect(screen.queryByText("EXPOSURE RECONCILED")).not.toBeInTheDocument();
  });

  it("clears a positive status when a later projection refresh fails", async () => {
    let statusCalls = 0;
    const intervalCallbacks: (() => void | Promise<void>)[] = [];
    vi.spyOn(globalThis, "setInterval").mockImplementation((callback) => {
      intervalCallbacks.push(callback as () => void);
      return 1 as unknown as ReturnType<typeof setInterval>;
    });
    vi.spyOn(globalThis, "clearInterval").mockImplementation(() => {});
    deskFetch.mockImplementation((path: string) => {
      if (path === "/desk/broker/status" && statusCalls++ > 0) return Promise.reject(new Error("status service unavailable"));
      return Promise.resolve(projections({
        state: "RECONCILED",
        last_reconciled_at: "2026-10-18T10:00:00Z",
        last_stream_event_at: "2026-10-18T10:00:00Z",
        stream_connected: true,
        generation: 12,
        divergence_count: 0,
        failure_reason: null,
      })[path]);
    });

    render(<DeskPositions />);
    expect(await screen.findByText("EXPOSURE RECONCILED")).toBeInTheDocument();
    await act(async () => intervalCallbacks[0]());

    expect(await screen.findByText("RECONCILIATION UNAVAILABLE")).toHaveClass("warn");
      expect(screen.getAllByText(/Refresh failed: status service unavailable/).length).toBeGreaterThan(0);
    expect(screen.queryByText("EXPOSURE RECONCILED")).not.toBeInTheDocument();
    expect(screen.getByText("BROKER UNAVAILABLE")).toHaveClass("warn");
  });
});
