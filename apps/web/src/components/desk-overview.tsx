"use client";

import { BriefcaseBusiness, LockKeyhole, Radar, ShieldCheck, TicketCheck } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { StateCard } from "@/components/workspace-shell";
import { deskFetch, IdentityView } from "@/lib/api";

type Workspace = { status: string; scanner_enabled: boolean; watchlist_count: number };
type Account = { equity: string; last_equity: string } | null;
type Broker = { state: string; stream_connected: boolean; last_reconciled_at: string | null };
type Opportunity = { symbol?: string; disposition?: string; observed_at?: string };
type Approval = { state?: string };
type Position = { quantity?: string | number };
type Order = { status?: string };

const ACTIVE_APPROVAL_STATES = new Set(["APPROVED_FOR_SESSION", "REVALIDATING", "READY_TO_SUBMIT"]);
const WORKING_ORDER_STATUSES = new Set(["new", "accepted", "pending_new", "partially_filled"]);
const OPPORTUNITY_LABELS: [string, string][] = [["NO_TRADE", "no trade"], ["TRADE", "trade"], ["PRE_SCAN_CANDIDATE", "pre-scan candidate"], ["RESEARCH_CANDIDATE", "research candidate"], ["RISK_REJECTED", "risk rejected"]];

function normalizeState(value: string | undefined): string { return (value ?? "").trim().replaceAll(" ", "_").toUpperCase(); }
function readableDisposition(value: string): string { return value.toLowerCase().replaceAll("_", " "); }

export function opportunitySummary(records: Opportunity[] | null, watchlistCount: number): string {
  if (records === null) return "— unavailable";
  const latest = new Map<string, Opportunity>();
  for (const record of records) {
    const symbol = record.symbol?.trim().toUpperCase();
    if (!symbol) continue;
    const current = latest.get(symbol);
    if (!current) { latest.set(symbol, record); continue; }
    const currentTime = current.observed_at ? Date.parse(current.observed_at) : Number.NaN;
    const nextTime = record.observed_at ? Date.parse(record.observed_at) : Number.NaN;
    if (Number.isFinite(nextTime) && (!Number.isFinite(currentTime) || nextTime > currentTime)) latest.set(symbol, record);
  }
  const counts = new Map<string, number>();
  for (const record of latest.values()) { const state = normalizeState(record.disposition); counts.set(state, (counts.get(state) ?? 0) + 1); }
  const tracked = Math.max(Number.isFinite(watchlistCount) ? watchlistCount : 0, latest.size);
  const unavailableStates = [...counts.keys()].filter((state) => state.startsWith("UNAVAILABLE"));
  const unavailable = Math.max(tracked - latest.size, 0) + unavailableStates.reduce((total, state) => total + (counts.get(state) ?? 0), 0);
  const parts = [`${tracked} symbols tracked`];
  for (const [state, label] of OPPORTUNITY_LABELS) if (counts.get(state)) parts.push(`${counts.get(state)} ${label}`);
  if (unavailable) parts.push(`${unavailable} unavailable`);
  for (const [state, count] of counts) if (!OPPORTUNITY_LABELS.some(([known]) => known === state) && !state.startsWith("UNAVAILABLE") && count) parts.push(`${count} ${readableDisposition(state)}`);
  return parts.join(" · ");
}

export function approvalSummary(records: Approval[] | null): string {
  if (records === null) return "— unavailable";
  let active = 0; let filled = 0; let failed = 0;
  for (const record of records) { const state = normalizeState(record.state); if (ACTIVE_APPROVAL_STATES.has(state)) active += 1; else if (state === "FILLED") filled += 1; else failed += 1; }
  return `${active} active · ${filled} filled · ${failed} failed`;
}

function numericQuantity(value: string | number | undefined): number | null { const quantity = typeof value === "number" ? value : Number(value); return Number.isFinite(quantity) ? quantity : null; }

export function positionsOrdersSummary(positions: Position[] | null, orders: Order[] | null): string {
  const positionCount = positions === null ? "—" : String(positions.filter((position) => { const quantity = numericQuantity(position.quantity); return quantity !== null && quantity !== 0; }).length);
  const orderCount = orders === null ? "—" : String(orders.filter((order) => WORKING_ORDER_STATUSES.has((order.status ?? "").toLowerCase())).length);
  return `${positionCount} active positions · ${orderCount} working orders`;
}

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
function formatCurrency(value: string | number): string { const amount = Number(value); return Number.isFinite(amount) ? currency.format(amount) : "—"; }
function profitAndLoss(account: Account): { value: string; tone: "neutral" | "good" | "warn" } {
  if (!account) return { value: "—", tone: "neutral" };
  const equity = Number(account.equity); const lastEquity = Number(account.last_equity);
  if (!Number.isFinite(equity) || !Number.isFinite(lastEquity)) return { value: "—", tone: "neutral" };
  const value = equity - lastEquity; return { value: currency.format(value), tone: value > 0 ? "good" : value < 0 ? "warn" : "neutral" };
}

export function DeskOverview() {
  const router = useRouter();
  const [data, setData] = useState<{ identity: IdentityView; workspace: Workspace; account: Account; broker: Broker; summaries: { opportunities: string; approvals: string; positions: string } } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const identity = await deskFetch<IdentityView>("/identity/me");
        if (!identity.workspace_id) { if (identity.is_admin) { router.replace("/admin"); return; } throw new Error("No Connected Paper Workspace has been provisioned for this account."); }
        const [workspace, account, broker] = await Promise.all([deskFetch<Workspace>("/desk/workspace"), deskFetch<Account>("/desk/broker/account"), deskFetch<Broker>("/desk/broker/status")]);
        const [opportunities, approvals, positions, orders] = await Promise.allSettled([deskFetch<Opportunity[]>("/desk/opportunities"), deskFetch<Approval[]>("/desk/approvals"), deskFetch<Position[]>("/desk/broker/positions"), deskFetch<Order[]>("/desk/broker/orders")]);
        if (active) setData({ identity, workspace, account, broker, summaries: {
          opportunities: opportunitySummary(opportunities.status === "fulfilled" ? opportunities.value : null, workspace.watchlist_count),
          approvals: approvalSummary(approvals.status === "fulfilled" ? approvals.value : null),
          positions: positionsOrdersSummary(positions.status === "fulfilled" ? positions.value : null, orders.status === "fulfilled" ? orders.value : null),
        } });
      } catch (caught) { if (active) setError(caught instanceof Error ? caught.message : "Workspace unavailable"); }
    }
    void load(); return () => { active = false; };
  }, [router]);
  if (error) return <section className="mode-banner danger"><LockKeyhole/><div><strong>Connected workspace unavailable</strong><span>{error}</span></div></section>;
  if (!data) return <section className="loading-panel">Loading tenant-scoped broker state…</section>;
  const pnl = profitAndLoss(data.account);
  return <>
    <div className="state-grid"><StateCard title="PAPER EQUITY" value={data.account ? formatCurrency(data.account.equity) : "—"} detail="Broker-confirmed only"/><StateCard title="TOTAL P/L" value={pnl.value} detail="Equity minus last equity" tone={pnl.tone}/><StateCard title="BROKER STATE" value={data.broker.state} detail={data.broker.stream_connected ? "trade_updates connected" : "stream unavailable"} tone={data.broker.state === "RECONCILED" ? "good" : "warn"}/><StateCard title="SCANNER" value={data.workspace.scanner_enabled ? "ENABLED" : "OFF"} detail={`${data.workspace.watchlist_count} / 50 symbols`}/></div>
    <div className="mode-banner blue"><ShieldCheck/><div><strong>Workspace boundary active for {data.identity.email}</strong><span>No browser-supplied workspace ID is accepted. Account, events, orders, and Guardian state are server-derived from this identity.</span></div></div>
    <div className="panel-grid three"><Link className="control-panel action-panel" href="/desk/scanner"><Radar/><h3>Discover opportunities</h3><p>Review candidates found from your workspace watchlist.</p><span>{data.summaries.opportunities} · Open scanner</span></Link><Link className="control-panel action-panel" href="/desk/approvals"><TicketCheck/><h3>Pre-approved</h3><p>Review paper trades approved for the current session.</p><span>{data.summaries.approvals} · Open approvals</span></Link><Link className="control-panel action-panel" href="/desk/positions"><BriefcaseBusiness/><h3>Positions &amp; Orders</h3><p>Inspect broker-confirmed positions and paper orders.</p><span>{data.summaries.positions} · Open positions</span></Link></div>
  </>;
}

export { profitAndLoss };
