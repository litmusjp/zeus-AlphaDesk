"use client";

import { FormEvent, useEffect, useState } from "react";
import { Bot, CheckCircle2, KeyRound, LockKeyhole, Trash2 } from "lucide-react";

import { deskFetch } from "@/lib/api";

type Provider = "alpaca-paper" | "alpaca-live" | "openrouter" | "anthropic";
type Status = {
  provider: string;
  configured: boolean;
  enabled: boolean;
  validation_status: string;
  fingerprint: string | null;
  configuration: Record<string, unknown>;
  validated_at: string | null;
};

export function CredentialSettings() {
  const [statuses, setStatuses] = useState<Status[]>([]);
  const [alpacaPaperKey, setAlpacaPaperKey] = useState("");
  const [alpacaPaperSecret, setAlpacaPaperSecret] = useState("");
  const [alpacaLiveKey, setAlpacaLiveKey] = useState("");
  const [alpacaLiveSecret, setAlpacaLiveSecret] = useState("");
  const [openRouterKey, setOpenRouterKey] = useState("");
  const [openRouterModel, setOpenRouterModel] = useState("openai/gpt-4.1-mini");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [anthropicModel, setAnthropicModel] = useState("claude-sonnet-4-5-20250929");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setStatuses(await deskFetch<Status[]>("/desk/credentials"));
  }

  useEffect(() => {
    void deskFetch<Status[]>("/desk/credentials")
      .then(setStatuses)
      .catch((error: Error) => setMessage(error.message));
  }, []);

  async function submit(event: FormEvent, provider: Provider, save: boolean) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const payload = provider === "alpaca-paper"
        ? { api_key_id: alpacaPaperKey, secret_key: alpacaPaperSecret }
        : provider === "alpaca-live"
          ? { api_key_id: alpacaLiveKey, secret_key: alpacaLiveSecret }
        : provider === "openrouter"
          ? { api_key: openRouterKey, model: openRouterModel }
          : { api_key: anthropicKey, model: anthropicModel };
      const credentialEndpoint = provider === "alpaca-paper" ? "alpaca" : provider;
      const endpoint = `/desk/credentials/${credentialEndpoint}${save ? "" : "/test"}`;
      await deskFetch(endpoint, { method: save ? "PUT" : "POST", body: JSON.stringify(payload) });
      if (save) {
        if (provider === "alpaca-paper") {
          setAlpacaPaperKey("");
          setAlpacaPaperSecret("");
        } else if (provider === "alpaca-live") {
          setAlpacaLiveKey("");
          setAlpacaLiveSecret("");
        } else if (provider === "openrouter") {
          setOpenRouterKey("");
        } else {
          setAnthropicKey("");
        }
        await refresh();
      }
      const label = provider === "alpaca-paper" ? "Alpaca paper" : provider === "alpaca-live" ? "Alpaca live" : provider === "openrouter" ? "OpenRouter" : "Anthropic";
      setMessage(`${label} ${save ? "verified and encrypted" : "validation passed"}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Validation failed");
    } finally {
      setBusy(false);
    }
  }

  async function remove(provider: Provider) {
    setBusy(true);
    setMessage("");
    try {
      const credentialEndpoint = provider === "alpaca-paper" ? "alpaca" : provider;
      await deskFetch(`/desk/credentials/${credentialEndpoint}`, { method: "DELETE" });
      await refresh();
      setMessage("Credential removed and dependent access disabled.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  }

  const alpacaPaper = statuses.find((status) => status.provider === "ALPACA_PAPER");
  const alpacaLive = statuses.find((status) => status.provider === "ALPACA_LIVE");
  const openRouter = statuses.find((status) => status.provider === "OPENROUTER");
  const anthropic = statuses.find((status) => status.provider === "ANTHROPIC");

  return (
    <>
      <div className="mode-banner blue">
        <LockKeyhole />
        <div>
          <strong>Credentials are write-only.</strong>
          <span>Secrets are encrypted with tenant-bound AES-256-GCM and are never returned to this browser after saving.</span>
        </div>
      </div>
      <p className="form-message">{message}</p>
      <div className="settings-grid">
        <AlpacaCredentialCard environment="paper" status={alpacaPaper} apiKey={alpacaPaperKey} secret={alpacaPaperSecret} busy={busy} onKeyChange={setAlpacaPaperKey} onSecretChange={setAlpacaPaperSecret} onSubmit={(event, save) => void submit(event, "alpaca-paper", save)} onRemove={() => void remove("alpaca-paper")} />
        <AlpacaCredentialCard environment="live" status={alpacaLive} apiKey={alpacaLiveKey} secret={alpacaLiveSecret} busy={busy} onKeyChange={setAlpacaLiveKey} onSecretChange={setAlpacaLiveSecret} onSubmit={(event, save) => void submit(event, "alpaca-live", save)} onRemove={() => void remove("alpaca-live")} />

        <AICredentialCard
          provider="openrouter"
          title="OpenRouter"
          description="Read-only AI analysis"
          icon={<Bot />}
          status={openRouter}
          apiKey={openRouterKey}
          model={openRouterModel}
          modelLabel="OpenRouter model"
          onApiKeyChange={setOpenRouterKey}
          onModelChange={setOpenRouterModel}
          onSubmit={(event, save) => void submit(event, "openrouter", save)}
          onRemove={() => void remove("openrouter")}
          busy={busy}
        />

        <AICredentialCard
          provider="anthropic"
          title="Anthropic"
          description="Read-only AI analysis"
          icon={<Bot />}
          status={anthropic}
          apiKey={anthropicKey}
          model={anthropicModel}
          modelLabel="Anthropic model"
          onApiKeyChange={setAnthropicKey}
          onModelChange={setAnthropicModel}
          onSubmit={(event, save) => void submit(event, "anthropic", save)}
          onRemove={() => void remove("anthropic")}
          busy={busy}
        />
      </div>
    </>
  );
}

function AlpacaCredentialCard({ environment, status, apiKey, secret, busy, onKeyChange, onSecretChange, onSubmit, onRemove }: { environment: "paper" | "live"; status: Status | undefined; apiKey: string; secret: string; busy: boolean; onKeyChange: (value: string) => void; onSecretChange: (value: string) => void; onSubmit: (event: FormEvent, save: boolean) => void; onRemove: () => void }) {
  const live = environment === "live";
  return <form className="credential-card" onSubmit={(event) => onSubmit(event, true)}>
    <div className="provider-title"><KeyRound /><div><h2>Alpaca {live ? "Live" : "Paper"}</h2><p>{live ? "Live endpoint — dangerous mode" : "Paper endpoint"}</p></div><StatusPill status={status} /></div>
    {live ? <div className="mode-banner danger"><LockKeyhole /><div><strong>LIVE can send real orders.</strong><span>This credential is separate from paper and never selected implicitly.</span></div></div> : null}
    <StoredStatus status={status} />
    <label>{live ? "Live" : "Paper"} API key ID<input required minLength={8} autoComplete="off" value={apiKey} onChange={(event) => onKeyChange(event.target.value)} /></label>
    <label>{live ? "Live" : "Paper"} API secret<input required minLength={8} type="password" autoComplete="new-password" value={secret} onChange={(event) => onSecretChange(event.target.value)} /></label>
    <div className="button-row"><button type="button" className="secondary-button" disabled={busy || !apiKey || !secret} onClick={(event) => onSubmit(event as unknown as FormEvent, false)}>Test {live ? "live" : "paper"} connection</button><button disabled={busy}>Test &amp; save encrypted</button>{status?.configured ? <button type="button" className="icon-button" aria-label={`Delete Alpaca ${environment} credentials`} onClick={onRemove}><Trash2 /></button> : null}</div>
  </form>;
}

function AICredentialCard({
  title,
  description,
  icon,
  status,
  apiKey,
  model,
  modelLabel,
  onApiKeyChange,
  onModelChange,
  onSubmit,
  onRemove,
  busy,
}: {
  provider: Provider;
  title: string;
  description: string;
  icon: React.ReactNode;
  status: Status | undefined;
  apiKey: string;
  model: string;
  modelLabel: string;
  onApiKeyChange: (value: string) => void;
  onModelChange: (value: string) => void;
  onSubmit: (event: FormEvent, save: boolean) => void;
  onRemove: () => void;
  busy: boolean;
}) {
  const active = Boolean(status?.configuration.active);
  return (
    <form className="credential-card" onSubmit={(event) => onSubmit(event, true)}>
      <div className="provider-title"><span>{icon}</span><div><h2>{title}</h2><p>{description}</p></div><StatusPill status={status} /></div>
      <StoredStatus status={status} />
      {active ? <p className="form-message">Active provider for AI analysis.</p> : null}
      <label>API key<input required minLength={8} type="password" autoComplete="new-password" value={apiKey} onChange={(event) => onApiKeyChange(event.target.value)} /></label>
      <label>{modelLabel}<input required minLength={2} maxLength={160} value={model} onChange={(event) => onModelChange(event.target.value)} /></label>
      <div className="button-row">
        <button type="button" className="secondary-button" disabled={busy || !apiKey || !model} onClick={(event) => onSubmit(event as unknown as FormEvent, false)}>Test provider</button>
        <button disabled={busy}>Test &amp; save encrypted</button>
        {status?.configured ? <button type="button" className="icon-button" aria-label={`Delete ${title} credentials`} onClick={onRemove}><Trash2 /></button> : null}
      </div>
    </form>
  );
}

function StoredStatus({ status }: { status: Status | undefined }) {
  return status?.configured ? <p className="masked-record"><CheckCircle2 /> Stored fingerprint: <code>{status.fingerprint}</code> · {String(status.configuration.model ?? status.validation_status)}</p> : null;
}

function StatusPill({ status }: { status: Status | undefined }) {
  return <span className={status?.enabled ? "status-pill good" : "status-pill"}>{status?.enabled ? "VERIFIED" : "NOT CONNECTED"}</span>;
}
