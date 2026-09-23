"use client";

import {
  Database,
  LockKeyhole,
  ShieldCheck,
  Star,
} from "lucide-react";
import { useEffect, useState } from "react";

import { deskFetch, IdentityView } from "@/lib/api";
import { AgentApiPage } from "./agent-api";
import { CredentialSettings } from "./credential-settings";

type ProvisionedWorkspace = {
  workspace_id: string;
  status: string;
  created: boolean;
  watchlist_count: number;
};

type TradingEnvironment = { environment: "PAPER" | "LIVE"; live_confirmation_phrase: string; dangerous_warning: string; preparation_state?: string | null; prepared_at?: string | null; target_account_id?: string | null };

export function AdminConsole() {
  const [identity, setIdentity] = useState<IdentityView | null>(null);
  const [workspace, setWorkspace] = useState<ProvisionedWorkspace | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [tradingEnvironment, setTradingEnvironment] = useState<TradingEnvironment | null>(null);
  const [confirmation, setConfirmation] = useState("");

  useEffect(() => {
    deskFetch<IdentityView>("/identity/me")
      .then(setIdentity)
      .catch((error: Error) => setMessage(error.message));
  }, []);

  useEffect(() => {
    if (identity?.is_admin && identity.workspace_id) {
      void deskFetch<TradingEnvironment>("/admin/workspace/trading-environment")
        .then(setTradingEnvironment)
        .catch((error: Error) => setMessage(error.message));
    }
  }, [identity]);

  async function changeEnvironment(environment: "PAPER" | "LIVE") {
    setBusy(true);
    try {
      const result = await deskFetch<TradingEnvironment>("/admin/workspace/trading-environment", {
        method: "PUT",
        body: JSON.stringify({ environment, confirmation: environment === "LIVE" ? confirmation : null }),
      });
      setTradingEnvironment(result);
      setConfirmation("");
      setMessage(`Trading environment is now ${result.environment}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Trading environment change failed");
    } finally {
      setBusy(false);
    }
  }

  async function prepareLive() {
    setBusy(true);
    try {
      const result = await deskFetch<TradingEnvironment>("/admin/workspace/trading-environment/prepare-live", { method: "POST" });
      setTradingEnvironment(result);
      setMessage("LIVE target prepared. The workspace remains PAPER and no orders were created.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "LIVE preparation failed closed");
    } finally {
      setBusy(false);
    }
  }

  async function provision() {
    setBusy(true);
    setMessage("");
    try {
      const result = await deskFetch<ProvisionedWorkspace>("/admin/workspace", {
        method: "POST",
      });
      setWorkspace(result);
      setIdentity((current) => current ? {
        ...current,
        workspace_id: result.workspace_id,
        workspace_status: result.status,
      } : current);
      setMessage(result.created
        ? "Your Connected Paper Workspace is ready for provider setup."
        : "Your existing Connected Paper Workspace is ready.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Workspace provisioning failed");
    } finally {
      setBusy(false);
    }
  }

  if (!identity && !message) return <section className="loading-panel">Loading administrator state…</section>;
  if (!identity) return <p className="form-message" role="alert">{message}</p>;
  if (!identity.is_admin) return <section className="mode-banner danger"><LockKeyhole/><div><strong>Administrator access required</strong><span>This identity cannot manage invitations or provision administrator workspaces.</span></div></section>;

  const hasWorkspace = Boolean(identity.workspace_id || workspace);
  return (
    <div className="admin-console-sections">
      <section className="admin-provision-panel control-panel" aria-labelledby="admin-access-heading">
        <div className="admin-access-heading">
          <span><ShieldCheck /></span>
          <div><small>ADMINISTRATOR ACCESS</small><h2 id="admin-access-heading">Active</h2><p>Signed in with protected platform-administration rights.</p></div>
        </div>
        {!hasWorkspace ? (
          <>
            <div className="admin-provision-copy"><h2>Create your Paper Workspace</h2><p>No Connected Paper Workspace has been provisioned for this account. Create a tenant-isolated workspace to enable operator controls.</p></div>
            <ul className="admin-provision-list">
              <li><ShieldCheck/><span><strong>Protected onboarding</strong>Administrator authorization is checked again by the API.</span></li>
              <li><Star/><span><strong>ONBOARDING outcome</strong>The default liquid-options watchlist is applied.</span></li>
              <li><LockKeyhole/><span><strong>No invitation required</strong>No invitation use is consumed.</span></li>
              <li><Database/><span><strong>No new identity</strong>Your existing Supabase identity and admin role are retained.</span></li>
            </ul>
            <button className="admin-provision-button" disabled={busy} onClick={provision}><LockKeyhole/>{busy ? "Creating workspace…" : "Create my Paper Workspace"}</button>
          </>
        ) : null}
        {message ? <p className="form-message" role="status">{message}</p> : null}
      </section>
      <section className="admin-console-section" aria-labelledby="credential-settings-heading">
        {tradingEnvironment ? <section className="control-panel" aria-labelledby="trading-environment-heading">
          <h2 id="trading-environment-heading">Trading environment</h2>
          <p className={tradingEnvironment.environment === "LIVE" ? "mode-banner danger" : "mode-banner blue"}>
            {tradingEnvironment.environment === "LIVE" ? tradingEnvironment.dangerous_warning : tradingEnvironment.preparation_state === "PREPARED" ? "LIVE target prepared only. PAPER remains active; the worker must establish its own live stream before execution." : "PAPER is the default and remains off for live trading."}
          </p>
          <div className="button-row"><button disabled={busy || tradingEnvironment.environment === "LIVE"} onClick={() => void prepareLive()}>Prepare LIVE</button><button disabled={busy || tradingEnvironment.environment === "PAPER"} onClick={() => void changeEnvironment("PAPER")}>Switch to PAPER</button><button disabled={busy || tradingEnvironment.environment === "LIVE" || tradingEnvironment.preparation_state !== "PREPARED"} onClick={() => void changeEnvironment("LIVE")}>Enable LIVE</button></div>
          {tradingEnvironment.environment === "PAPER" ? <label className="trading-confirmation"><span>Type <code>{tradingEnvironment.live_confirmation_phrase}</code> to enable LIVE</span><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label> : null}
          {tradingEnvironment.preparation_state === "PREPARED" ? <p className="form-message">Prepared account: <code>{tradingEnvironment.target_account_id}</code>. Preparation expires quickly and never enables execution by itself.</p> : null}
        </section> : null}
        <h2 id="credential-settings-heading">Credential Settings</h2>
        <CredentialSettings />
      </section>
      <section className="admin-console-section" aria-labelledby="agent-api-heading">
        <h2 id="agent-api-heading">Agent API &amp; MCP</h2>
        <AgentApiPage />
      </section>
    </div>
  );
}
