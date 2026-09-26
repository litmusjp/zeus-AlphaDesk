"use client";

import { AlertTriangle, CheckCircle2, Clock3, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { deskFetch } from "@/lib/api";

type Leg = { side: string; ratio: number; contract?: { symbol?: string; strike?: string; option_type?: string } };
type Structure = { structure_type?: string; quantity?: number; net_premium_per_share?: string; max_loss?: string; max_profit?: string; break_evens?: string[]; legs?: Leg[] };
type Candidate = { structure?: Structure };
type RiskCheck = { name: string; passed: boolean };
type RiskDecision = { decision?: string; checks?: RiskCheck[] };
type OrderIntent = { client_order_id: string; quantity: number; limit_price: string };
type OptionDiagnostics = { total_contracts: number; requested_type_contracts: number; strict_eligible_contracts: number; selected_contracts: number; rejection_counts: Record<string, number> };
type Analysis = { opportunity_id: string; symbol: string; disposition: string; source: string; observed_at: string; expires_at: string; signal: Record<string, unknown>; candidate: Candidate | null; risk_decision: RiskDecision | null; order_intent: OrderIntent | null; option_diagnostics: OptionDiagnostics | null; reason_codes: string[] };
type ExitPlan = { stop_loss: string; profit_target: string | null; expires_at: string; stale_data_behavior: "FAIL_CLOSED"; };
type ConditionalApproval = { approval_id: string; opportunity_id: string; state: string; session_date: string; approved_at: string; expires_at: string; max_limit_price: string; max_loss: string; max_quantity: number; max_quote_age_seconds: number; failure_reason: string | null; exit_plan: ExitPlan | null };
type Workspace = { trading_environment: "PAPER" | "LIVE" };
type NextSessionExit = { available: boolean; session_date: string | null; session_open: string | null; session_close: string | null; recommended_exit_at: string | null; timezone: string; unavailable_reason: string | null };

function exchangeDateTimeLocal(instant: string, timeZone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date(instant));
  const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
}

function exchangeDateTimeToISOString(value: string, timeZone: string) {
  const [datePart, timePart] = value.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  let candidate = new Date(Date.UTC(year, month - 1, day, hour, minute));
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(candidate);
    const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, Number(part.value)]));
    const displayed = Date.UTC(values.year, values.month - 1, values.day, values.hour, values.minute);
    const wanted = Date.UTC(year, month - 1, day, hour, minute);
    candidate = new Date(candidate.getTime() + wanted - displayed);
  }
  return candidate.toISOString();
}

export function OrderReview({ id }: { id: string }) {
  const [opportunity, setOpportunity] = useState<Analysis | null>(null);
  const [approval, setApproval] = useState<ConditionalApproval | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState<Date | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [nextSessionExit, setNextSessionExit] = useState<NextSessionExit | null>(null);
  const [liveConfirmation, setLiveConfirmation] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [profitTarget, setProfitTarget] = useState("");
  const [exitExpiry, setExitExpiry] = useState("");

  const refresh = useCallback(async () => {
    const [nextOpportunity, approvals, nextWorkspace, nextExit] = await Promise.all([
      deskFetch<Analysis>(`/desk/opportunities/${id}`),
      deskFetch<ConditionalApproval[]>("/desk/approvals"),
      deskFetch<Workspace>("/desk/workspace"),
      deskFetch<NextSessionExit>("/desk/next-session-exit"),
    ]);
    setOpportunity(nextOpportunity);
    setApproval(approvals.find((item) => item.opportunity_id === id) ?? null);
    setWorkspace(nextWorkspace);
    setNextSessionExit(nextExit);
    if (nextOpportunity.candidate?.structure?.max_loss) setStopLoss((current) => current || nextOpportunity.candidate!.structure!.max_loss!);
    if (nextOpportunity.candidate?.structure?.max_profit) setProfitTarget((current) => current || nextOpportunity.candidate!.structure!.max_profit!);
    if (nextExit.available && nextExit.recommended_exit_at) setExitExpiry((current) => current || exchangeDateTimeLocal(nextExit.recommended_exit_at!, nextExit.timezone));
  }, [id]);

  useEffect(() => {
    const kickoff = setTimeout(() => {
      void refresh().catch((error: Error) => setMessage(error.message));
    }, 0);
    return () => clearTimeout(kickoff);
  }, [refresh]);

  useEffect(() => {
    const kickoff = setTimeout(() => setNow(new Date()), 0);
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => {
      clearTimeout(kickoff);
      clearInterval(timer);
    };
  }, []);

  if (!opportunity) return <section className="loading-panel">{message || "Loading immutable order review…"}</section>;

  const structure = opportunity.candidate?.structure;
  const intent = opportunity.order_intent;
  const checks = opportunity.risk_decision?.checks ?? [];
  const recommendedQuantity = intent?.quantity ?? structure?.quantity;
  const recommendedLimit = intent?.limit_price ?? structure?.net_premium_per_share;
  const canApprove = opportunity.source === "ALPACA_REAL" && ["TRADE", "PRE_SCAN_CANDIDATE"].includes(opportunity.disposition) && opportunity.risk_decision?.decision === "APPROVE" && now !== null && new Date(opportunity.expires_at).getTime() > now.getTime() && Boolean(structure) && recommendedQuantity !== undefined && recommendedLimit !== undefined && Boolean(stopLoss) && Boolean(exitExpiry);
  const canRenewApproval = !approval || ["EXPIRED", "CONDITION_FAILED", "REJECTED"].includes(approval.state) || (approval.state === "APPROVED_FOR_SESSION" && now !== null && new Date(approval.expires_at).getTime() <= now.getTime());
  const approvalCanReject = approval?.state === "APPROVED_FOR_SESSION" && now !== null && new Date(approval.expires_at).getTime() > now.getTime();

  async function approveForSession() {
    if (!structure || recommendedQuantity === undefined || recommendedLimit === undefined) return;
    setBusy(true);
    setMessage("");
    try {
      const nextApproval = await deskFetch<ConditionalApproval>(`/desk/opportunities/${id}/approve-session`, {
        method: "POST",
        body: JSON.stringify({
          max_limit_price: recommendedLimit,
          max_loss: structure.max_loss,
          max_quantity: recommendedQuantity,
          max_quote_age_seconds: 30,
          exit_plan: { stop_loss: stopLoss, profit_target: profitTarget || null, expires_at: exchangeDateTimeToISOString(exitExpiry, nextSessionExit?.timezone ?? "America/New_York"), stale_data_behavior: "FAIL_CLOSED" },
          live_order_confirmation: workspace?.trading_environment === "LIVE" ? liveConfirmation : null,
        }),
      });
      setApproval(nextApproval);
      setMessage(`Approved for the next U.S. session (${nextApproval.session_date}). The worker will revalidate before submitting.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Conditional approval failed");
    } finally {
      setBusy(false);
    }
  }

  async function rejectApproval() {
    if (!approval) return;
    setBusy(true);
    try {
      await deskFetch(`/desk/approvals/${approval.approval_id}/reject`, { method: "POST" });
      await refresh();
      setMessage("Conditional approval rejected.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Approval rejection failed");
    } finally {
      setBusy(false);
    }
  }

  return <>
    <div className={`mode-banner ${canApprove ? "blue" : "danger"}`}>
      {canApprove ? <ShieldCheck /> : <AlertTriangle />}
      <div><strong>{canApprove ? "Real-source intent ready for conditional approval" : "This opportunity cannot be approved"}</strong><span>{canApprove ? "Approve for the next U.S. session; the worker revalidates every gate before paper submission." : opportunity.reason_codes.join(", ") || "No approved immutable intent is present."}</span></div>
    </div>
    <div className="review-grid">
      <section className="control-panel">
        <div className="panel-title"><div><small>IMMUTABLE ORDER REVIEW</small><h2>{opportunity.symbol} · {String(structure?.structure_type ?? "No structure").replaceAll("_", " ")}</h2></div><span className="status-pill good">{opportunity.source}</span></div>
        <div className="review-stats"><div><small>Recommended quantity</small><strong>{recommendedQuantity ?? "—"}</strong></div><div><small>Recommended limit</small><strong>${recommendedLimit ?? "—"}</strong></div><div><small>Maximum loss</small><strong>${structure?.max_loss ?? "—"}</strong></div><div><small>Maximum profit</small><strong>${structure?.max_profit ?? "—"}</strong></div><div><small>Break-even</small><strong>{structure?.break_evens?.join(", ") ?? "—"}</strong></div><div><small>Quote expiry</small><strong>{new Date(opportunity.expires_at).toLocaleTimeString()}</strong></div></div>
        <div className="review-stats"><div><small>Signal score</small><strong>{String(opportunity.signal.score ?? "—")} / 100</strong></div><div><small>Disposition</small><strong>{opportunity.disposition.replaceAll("_", " ")}</strong></div></div>
        <h3>Decision reasons</h3><p>{opportunity.reason_codes.join(", ") || "All deterministic gates passed."}</p>
        {opportunity.option_diagnostics ? <><h3>Option-chain diagnostics</h3><p>{opportunity.option_diagnostics.selected_contracts} reviewable of {opportunity.option_diagnostics.requested_type_contracts} requested-side contracts; {opportunity.option_diagnostics.strict_eligible_contracts} pass current-session execution filters.</p><p>{Object.entries(opportunity.option_diagnostics.rejection_counts).map(([reason, count]) => `${reason.replaceAll("_", " ")}: ${count}`).join(" · ") || "No execution-filter rejections."}</p></> : null}
        <h3>Strategy legs</h3><div className="leg-list">{(structure?.legs ?? []).map((leg, index) => <div key={`${leg.contract?.symbol}-${index}`}><span>{leg.side} × {leg.ratio}</span><strong>{leg.contract?.symbol ?? `${leg.contract?.strike} ${leg.contract?.option_type}`}</strong></div>)}</div>
        <h3>Deterministic risk checks</h3><div className="check-grid">{checks.map((check) => <div key={check.name}><CheckCircle2 /><span>{check.name}</span><strong>{check.passed ? "PASS" : "FAIL"}</strong></div>)}</div>
      </section>
      <aside className="control-panel confirmation-panel">
        <Clock3 /><small>CONDITIONAL NEXT-SESSION APPROVAL</small>
        {approval ? <><strong>{approval.state.replaceAll("_", " ")}</strong><p>Session: {approval.session_date}<br />Maximum price: ${approval.max_limit_price}<br />Maximum loss: ${approval.max_loss}<br />Quote age: {approval.max_quote_age_seconds}s</p><p>Opening approval does not protect an active position unless an exit plan is present.</p>{approval.exit_plan ? <p>Exit plan: stop loss ${approval.exit_plan.stop_loss}{approval.exit_plan.profit_target ? ` · profit target $${approval.exit_plan.profit_target}` : ""} · expiry {new Date(approval.exit_plan.expires_at).toLocaleString()} · stale evidence: fail closed.</p> : <p className="form-message">No exit plan: this approval is not overnight-protected.</p>}{approval.failure_reason ? <p className="form-message">Reason: {approval.failure_reason}</p> : null}</> : <><p>Opening approval does not protect an active position unless the exit plan below is present. The worker revalidates every gate before submitting.</p><label>Maximum loss exit<input value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} inputMode="decimal" /></label><label>Profit target exit (optional)<input value={profitTarget} onChange={(event) => setProfitTarget(event.target.value)} inputMode="decimal" /></label><label>Time-based exit {nextSessionExit?.available ? <small>Recommended exchange-open exit: {nextSessionExit.session_date} {nextSessionExit.recommended_exit_at ? exchangeDateTimeLocal(nextSessionExit.recommended_exit_at, nextSessionExit.timezone) : "—"} ET</small> : null}<input aria-label="Time-based exit" type="datetime-local" value={exitExpiry} onChange={(event) => setExitExpiry(event.target.value)} /></label>{nextSessionExit && !nextSessionExit.available ? <p className="form-message">{nextSessionExit.unavailable_reason ?? "Next exchange-session exit unavailable; try again."}</p> : null}<p>Stale broker or market evidence: fail closed; no unguarded exit.</p></>}
        {workspace?.trading_environment === "LIVE" && canRenewApproval ? <label>Type ENABLE LIVE TRADING to confirm this exact first live order<input value={liveConfirmation} onChange={(event) => setLiveConfirmation(event.target.value)} /></label> : null}
        {approvalCanReject ? <button className="secondary-button" disabled={busy} onClick={() => void rejectApproval()}>Reject approval</button> : canRenewApproval ? <button disabled={!canApprove || busy || (workspace?.trading_environment === "LIVE" && liveConfirmation !== "ENABLE LIVE TRADING")} onClick={() => void approveForSession()}>Approve for next U.S. session</button> : null}
      </aside>
    </div>
    {message ? <p className="form-message">{message}</p> : null}
  </>;
}
