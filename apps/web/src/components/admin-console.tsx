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

export function AdminConsole() {
  const [identity, setIdentity] = useState<IdentityView | null>(null);
  const [workspace, setWorkspace] = useState<ProvisionedWorkspace | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    deskFetch<IdentityView>("/identity/me")
      .then(setIdentity)
      .catch((error: Error) => setMessage(error.message));
  }, []);

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
