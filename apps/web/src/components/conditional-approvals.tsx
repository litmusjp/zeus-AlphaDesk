"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Clock, ShieldAlert, XCircle } from "lucide-react";
import { deskFetch } from "@/lib/api";
import { StateCard } from "@/components/workspace-shell";

type Approval = {
  approval_id: string;
  opportunity_id: string | null;
  approval_kind: "OPEN" | "CLOSE";
  symbol: string;
  state: string;
  session_date: string;
  approved_at: string;
  expires_at: string;
  client_order_id: string;
  structure_fingerprint: string;
  max_limit_price: string;
  max_loss: string;
  max_quantity: number;
  max_quote_age_seconds: number;
  min_limit_price: string | null;
  position_asset_id: string | null;
  position_side: string | null;
  exit_order_side: string | null;
  broker_order_id: string | null;
  failure_reason: string | null;
};

function japanTime(value: string) {
  return new Date(value).toLocaleString("ja-JP", {
    timeZone: "Asia/Tokyo",
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function bound(approval: Approval) {
  if (approval.approval_kind === "CLOSE") {
    return approval.exit_order_side === "sell"
      ? `Minimum sell limit $${approval.min_limit_price}`
      : `Maximum buy limit $${approval.max_limit_price}`;
  }
  return `Maximum limit $${approval.max_limit_price}`;
}

export function ConditionalApprovals() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setApprovals(await deskFetch<Approval[]>("/desk/approvals"));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load approvals");
    }
  }, []);

  useEffect(() => {
    const kickoff = setTimeout(() => {
      void refresh();
    }, 0);
    const interval = setInterval(refresh, 10000);
    return () => {
      clearTimeout(kickoff);
      clearInterval(interval);
    };
  }, [refresh]);

  async function reject(approval: Approval) {
    setBusy(approval.approval_id);
    try {
      await deskFetch(`/desk/approvals/${approval.approval_id}/reject`, { method: "POST" });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approval rejection failed");
    } finally {
      setBusy(null);
    }
  }

  const active = approvals.filter((item) =>
    ["APPROVED_FOR_SESSION", "REVALIDATING", "READY_TO_SUBMIT", "SUBMITTING"].includes(item.state),
  ).length;
  const submitted = approvals.filter((item) =>
    ["SUBMITTED", "PARTIALLY_FILLED", "FILLED"].includes(item.state),
  ).length;
  const failed = approvals.filter((item) =>
    ["CONDITION_FAILED", "STALE", "EXPIRED", "BROKER_REJECTED", "REJECTED", "SUBMISSION_UNCERTAIN"].includes(item.state),
  ).length;

  return (
    <>
      <div className="mode-banner blue">
        <ShieldAlert />
        <div>
          <strong>Conditional approvals · paper only</strong>
          <span>Approvals are bounded to one U.S. session and revalidated against broker truth before any paper order.</span>
        </div>
      </div>
      <div className="state-grid" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
        <StateCard title="ACTIVE APPROVALS" value={`${active}`} detail="Awaiting next-session checks" tone={active ? "good" : "neutral"} />
        <StateCard title="SUBMITTED / FILLED" value={`${submitted}`} detail="Broker-linked lifecycle" tone="good" />
        <StateCard title="FAILED / CLOSED" value={`${failed}`} detail="Rejected, expired, or condition failed" tone={failed ? "warn" : "neutral"} />
      </div>
      {error ? <div className="mode-banner danger"><XCircle /><div><strong>Approval list error</strong><span>{error}</span></div></div> : null}
      <div className="positions-section-header" style={{ marginTop: "24px" }}>
        <div><small>OPERATOR REVIEW</small><h2>Pre-approved & Conditional Orders</h2></div>
        <span className="status-pill">JAPAN TIME DISPLAY</span>
      </div>
      <p style={{ fontSize: "12px", color: "#5a6864", margin: "4px 0 14px 0" }}>
        CLOSE approvals identify the exact contract and current position. They never substitute another QQQ contract.
      </p>
      <section className="data-table">
        <header style={{ gridTemplateColumns: "1.3fr .65fr 1.35fr .9fr 1.4fr 1.1fr .7fr" }}>
          <span>CONTRACT</span><span>ACTION</span><span>BOUND</span><span>QTY</span><span>NEXT SESSION / EXPIRY</span><span>STATE</span><span>ACTION</span>
        </header>
        {approvals.length ? approvals.map((approval) => {
          const canReject = approval.state === "APPROVED_FOR_SESSION";
          return <div className="data-row" key={approval.approval_id} style={{ gridTemplateColumns: "1.3fr .65fr 1.35fr .9fr 1.4fr 1.1fr .7fr" }}>
            <div><strong>{approval.symbol}</strong><small style={{ color: "#707d79", fontSize: "9px", display: "block" }}>{approval.approval_kind === "CLOSE" ? `Position ${approval.position_side ?? "—"} · ${approval.position_asset_id ?? "—"}` : approval.structure_fingerprint}</small></div>
            <span className={`status-pill ${approval.approval_kind === "CLOSE" ? "warn" : ""}`}>{approval.approval_kind}</span>
            <span>{bound(approval)}</span>
            <span>{approval.max_quantity}</span>
            <div><strong>{approval.session_date}</strong><small style={{ color: "#707d79", fontSize: "9px", display: "block" }}>{japanTime(approval.expires_at)} JST</small></div>
            <span className={`status-pill ${approval.state === "FILLED" ? "good" : ""}`}>{approval.state}</span>
            <div>{canReject ? <button className="secondary-button" style={{ fontSize: "10px", padding: "4px 7px" }} disabled={busy === approval.approval_id} onClick={() => reject(approval)}>{busy === approval.approval_id ? "…" : "Reject"}</button> : approval.broker_order_id ? <code style={{ fontSize: "9px" }}>{approval.broker_order_id.slice(0, 10)}…</code> : <Clock size={14} />}</div>
            {approval.failure_reason ? <small style={{ gridColumn: "1 / -1", color: "#a8372c" }}>Reason: {approval.failure_reason}</small> : null}
          </div>;
        }) : <div className="table-empty"><CheckCircle2 /><strong>No conditional approvals</strong><p>Pre-scan approvals and next-session position closes will appear here.</p></div>}
      </section>
    </>
  );
}
