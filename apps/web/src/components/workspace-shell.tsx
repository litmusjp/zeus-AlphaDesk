"use client";

import {
  Activity,
  BriefcaseBusiness,
  LayoutDashboard,
  LogOut,
  Radar,
  ShieldCheck,
  SlidersHorizontal,
  TicketCheck,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { deskFetch, IdentityView, supabaseBrowser } from "@/lib/api";

const icons = { command: LayoutDashboard, scanner: Radar, positions: BriefcaseBusiness, approvals: TicketCheck, guardian: ShieldCheck, assessment: SlidersHorizontal, invites: TicketCheck };

const deskNavigation = [
  ["Workspace Dashboard", "/desk", "command"],
  ["Market Scanner", "/desk/scanner", "scanner"],
  ["Pre-approved", "/desk/approvals", "approvals"],
  ["Positions & Orders", "/desk/positions", "positions"],
] as const;

const adminNavigation = [
  ["Admin Console", "/admin", "command"],
  ["Access & Invitations", "/admin/invitations", "invites"],
  ["Candidate Assessment", "/desk/assessment", "assessment"],
  ["Audit & Guardian", "/desk/audit", "guardian"],
] as const;

export function WorkspaceShell({ title, description, children }: Readonly<{ title: string; description: string; children: React.ReactNode }>) {
  const pathname = usePathname();
  const router = useRouter();
  const [identity, setIdentity] = useState<IdentityView | null>(null);

  useEffect(() => {
    deskFetch<IdentityView>("/identity/me").then(setIdentity).catch(() => setIdentity(null));
  }, []);

  async function signOut() {
    await supabaseBrowser()?.auth.signOut();
    router.push("/");
    router.refresh();
  }

  return <div className="workspace-shell mode-blue">
    <aside className="workspace-sidebar">
      <Link href="/" className="workspace-brand"><span>A</span><div><strong>AlphaDesk</strong><small>Connected paper options desk</small></div></Link>
      <div className="paper-only">PAPER ONLY</div>
      <nav aria-label="connected paper workspace navigation">
        {identity?.is_admin ? <span className="workspace-nav-label">ADMINISTRATION</span> : null}
        {identity?.is_admin ? adminNavigation.map(([label, href, icon]) => { const Icon = icons[icon]; const active = pathname === href || (href !== "/admin" && pathname.startsWith(`${href}/`)); return <Link key={href} className={active ? "workspace-link active" : "workspace-link"} href={href}><Icon size={18}/>{label}</Link>; }) : null}
        <span className="workspace-nav-label">CONNECTED PAPER</span>
        {deskNavigation.map(([label, href, icon]) => { const Icon = icons[icon]; const active = pathname === href || (href !== "/desk" && pathname.startsWith(`${href}/`)); const disabled = identity !== null && !identity.workspace_id; return disabled ? <span key={href} className="workspace-link disabled" aria-disabled="true"><Icon size={18}/>{label}</span> : <Link key={href} className={active ? "workspace-link active" : "workspace-link"} href={href}><Icon size={18}/>{label}</Link>; })}
      </nav>
      <div className="workspace-sidebar-spacer"/>
      <section className="workspace-status"><span className="mode-dot"/><small>{identity?.is_admin ? "ADMIN ACCESS ACTIVE" : "TENANT-ISOLATED WORKSPACE"}</small><strong>{identity?.is_admin && !identity.workspace_id ? "Operator controls inactive" : "Paper controls enforced"}</strong><p>{identity?.is_admin && !identity.workspace_id ? "Create your Paper Workspace to enable operator features." : "Execution always requires operator confirmation."}</p></section>
      <button className="sidebar-button" onClick={signOut}><LogOut size={16}/>Sign out</button>
    </aside>
    <main className="workspace-main">
      <header className="workspace-header"><div><h1>{title}</h1><p>{description}</p></div><div className="workspace-mode"><span>CONNECTED PAPER - REAL DATA / SIMULATED FUNDS</span><small><Activity size={14}/> PAPER ONLY</small></div></header>
      <div className="workspace-content">
        {children}
      </div>
    </main>
  </div>;
}

export function Stat({ label, value, detail }: Readonly<{ label: string; value: string; detail: string }>) {
  return <div className="stat"><small>{label}</small><strong>{value}</strong><span>{detail}</span></div>;
}

export function StateCard({ title, value, detail, tone = "neutral" }: Readonly<{ title: string; value: string; detail: string; tone?: "neutral" | "good" | "warn" | "bad" }>) {
  return <section className={`state-card ${tone}`}><small>{title}</small><strong>{value}</strong><p>{detail}</p></section>;
}
