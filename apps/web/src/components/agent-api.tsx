"use client";

import { Copy, KeyRound, RotateCw, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { deskFetch } from "@/lib/api";

type Key = { key_id: string; name: string; prefix: string; created_at: string; last_used_at: string | null; revoked_at: string | null };
type Created = Key & { secret: string };

export function AgentApiPage() {
  const [keys, setKeys] = useState<Key[]>([]); const [name, setName] = useState("My assessment agent"); const [secret, setSecret] = useState(""); const [message, setMessage] = useState("");
  async function load() { try { setKeys(await deskFetch<Key[]>("/desk/agent-api-keys")); } catch (error) { setMessage(error instanceof Error ? error.message : "Keys could not be loaded."); } }
  useEffect(() => {
    let active = true;
    deskFetch<Key[]>("/desk/agent-api-keys").then((value) => { if (active) setKeys(value); }).catch((error: unknown) => { if (active) setMessage(error instanceof Error ? error.message : "Keys could not be loaded."); });
    return () => { active = false; };
  }, []);
  async function create() { try { const value = await deskFetch<Created>("/desk/agent-api-keys", { method: "POST", body: JSON.stringify({ name }) }); setSecret(value.secret); setMessage("Copy this secret now. It will not be shown again."); await load(); } catch (error) { setMessage(error instanceof Error ? error.message : "Key could not be created."); } }
  async function action(id: string, verb: "revoke" | "rotate") { try { const value = await deskFetch<Created | Key>(`/desk/agent-api-keys/${id}/${verb}`, { method: "POST" }); if ("secret" in value) setSecret(value.secret); setMessage(verb === "rotate" ? "The old key was revoked. Copy the new secret now." : "Key revoked."); await load(); } catch (error) { setMessage(error instanceof Error ? error.message : "Key action failed."); } }
  return <div className="policy-guide-grid">
    <section className="panel"><div className="policy-warning"><ShieldCheck/><div><strong>Read-only boundary</strong><p>This endpoint returns a deterministic pass/fail assessment using the same workspace Candidate Assessment settings. A pass is not approval, an order intent, or permission to trade. Paper-only controls and human approval remain required.</p></div></div><h2>For an external agent</h2><p>Call <code>POST /api/v1/desk/strategy-assessments</code> with <code>X-AlphaDesk-API-Key</code>. Send the underlying, strategy type, legs, side, quantity, prices, Greeks and current evidence timestamps. The response includes <code>pass</code>, <code>decision</code>, stable check codes, policy snapshot and expiry.</p><p>MCP exposes one tool, <code>assess_options_strategy</code>. Configure <code>ALPHADESK_API_URL</code> and <code>ALPHADESK_API_KEY</code>. It forwards the request and contains no risk logic. Missing or stale evidence fails closed.</p></section>
    <section className="panel"><h2>Agent API keys</h2><p>Keys are tied to this workspace. AlphaDesk stores only a cryptographic hash. The secret is displayed once when created or rotated.</p><label className="policy-field"><span><strong>Key name</strong><small>Use a name that identifies the agent.</small></span><input value={name} onChange={(event) => setName(event.target.value)} /></label><button onClick={() => void create()}><KeyRound size={15}/> Create key</button>{secret ? <div className="form-message" role="status"><strong>Copy once:</strong> <code>{secret}</code><button aria-label="Copy API key" onClick={() => void navigator.clipboard.writeText(secret)}><Copy size={14}/></button></div> : null}{message ? <p className="form-message" role="status">{message}</p> : null}<ul>{keys.map((key) => <li key={key.key_id}><strong>{key.name}</strong> <code>{key.prefix}...</code> {key.revoked_at ? <span>revoked</span> : <><button onClick={() => void action(key.key_id, "rotate")}><RotateCw size={14}/> Rotate</button><button onClick={() => void action(key.key_id, "revoke")}><Trash2 size={14}/> Revoke</button></>}</li>)}</ul></section>
  </div>;
}
