import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConditionalApprovals } from "./conditional-approvals";

const deskFetch = vi.fn();
vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

function approval(overrides: Record<string, unknown> = {}) {
  return {
    approval_id: crypto.randomUUID(),
    opportunity_id: "opportunity-1",
    approval_kind: "OPEN",
    symbol: "QQQ",
    state: "FILLED",
    session_date: "2026-09-21",
    approved_at: "2026-09-20T00:00:00Z",
    expires_at: "2026-09-21T20:00:00Z",
    client_order_id: "client-order-1",
    structure_fingerprint: "QQQ-20260921-500C",
    max_limit_price: "12.00",
    max_loss: "100.00",
    max_quantity: 1,
    max_quote_age_seconds: 30,
    min_limit_price: null,
    position_asset_id: null,
    position_side: null,
    exit_order_side: null,
    broker_order_id: "broker-order-1",
    failure_reason: null,
    ...overrides,
  };
}

describe("ConditionalApprovals table layout", () => {
  it("keeps seven columns for CLOSE rows with failure reasons", async () => {
    deskFetch.mockResolvedValue([
      approval({ approval_id: "open-1" }),
      approval({ approval_id: "close-1", approval_kind: "CLOSE", state: "CONDITION_FAILED", opportunity_id: null, position_asset_id: "asset-1", position_side: "long", exit_order_side: "sell", failure_reason: "The live contract no longer matches the approved position." }),
      approval({ approval_id: "close-2", approval_kind: "CLOSE", state: "BROKER_REJECTED", opportunity_id: null, position_asset_id: "asset-2", position_side: "short", exit_order_side: "buy", failure_reason: "Broker rejected the close after revalidation." }),
      approval({ approval_id: "open-2" }),
      approval({ approval_id: "open-3" }),
      approval({ approval_id: "open-4" }),
      approval({ approval_id: "open-5" }),
    ]);

    render(<ConditionalApprovals />);
    const table = await screen.findByRole("heading", { name: "Pre-approved & Conditional Orders" }).then((heading) => heading.parentElement?.parentElement?.nextElementSibling?.nextElementSibling as HTMLElement);

    await waitFor(() => expect(table.querySelectorAll(".data-row")).toHaveLength(7));
    expect(table).toHaveClass("conditional-approvals-table");
    expect(table.querySelector("header")?.children).toHaveLength(7);
    for (const row of table.querySelectorAll(".data-row")) {
      expect(row.querySelectorAll(":scope > *:not(.failure-reason)")).toHaveLength(7);
      expect((row as HTMLElement).style.gridTemplateColumns).toBe("");
    }
    expect(table.querySelectorAll(".failure-reason")).toHaveLength(2);
  });
});
