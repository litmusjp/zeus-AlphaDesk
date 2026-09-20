# AlphaDesk project status

This branch makes the Connected Paper Workspace the sole supported application flow.

## Current milestone

Authenticated, invitation-controlled workspaces remain tenant-isolated and guarded by deterministic risk controls, explicit human confirmation, broker reconciliation, and an immutable persisted trading-environment boundary. PAPER is the default; LIVE is fail-closed behind admin-only readiness gates.

## Delivered capabilities

| Area | Current state |
|---|---|
| Foundation | FastAPI, worker, Next.js, PostgreSQL, Alembic, NATS JetStream, health checks, structured logging, and fail-closed `PAPER_ONLY` settings are implemented. |
| Public surface | Public demo API/session startup, chooser copy, demo links, and demo navigation are removed from the active runtime. Demo aliases return not-found. |
| Authentication | Hosted Supabase JWT login plus strict server-side invitation registration are implemented. Public Supabase signup is disabled. |
| Administration | Protected Admin Console, invitation creation/disablement, and invitation-free provisioning for an existing administrator identity are implemented. |
| Tenant isolation | Workspaces scope credentials, broker state, projections, intents, events, Guardian state, opportunities, scan runs, AI runs, audit state, and NATS subjects. Browser-supplied tenant IDs are not trusted. |
| Credential vault | Separate `ALPACA_PAPER` and `ALPACA_LIVE` identities plus AI-provider secrets are write-only and encrypted with versioned tenant-bound AES-256-GCM. |
| Alpaca state | The worker selects the persisted workspace environment, matching credentials, explicit `paper=True`/`paper=False` client, and environment-bound projections. LIVE has a separate admin-only PREPARED phase; preparation never changes PAPER or enables orders. |
| Options and risk | Bounded-risk structures, payoff calculations, break-even, Greeks, liquidity filtering, deterministic risk policy, and undefined-risk rejection are implemented and tested. |
| Market Scanner | 50-symbol watchlists, additive manual/AI discovery, checkbox removals, real Alpaca evidence, market countdown, closed-market guidance, no synthetic fallback, and tenant-scoped scan history are implemented. |
| AI workflow | BYOK provider integration, arbitrary model IDs, capability probe, strict schema validation, citations, retries/timeouts, response healing, and safe AI-only degradation are implemented. AI has no execution tools. |
| Paper execution | Immutable review, explicit acknowledgement, fresh deterministic preflight, stable client-order ID, tenant-bound submission, and uncertainty reconciliation are implemented. Unattended execution is absent. |
| Guardian/UI | Tenant-specific halt/recovery, broker projections, Connected Paper navigation, responsive market status, and scan history are implemented. |

## Intentional compatibility retention

The historical demo-session migration and ORM model remain so existing databases are not changed destructively. They are not initialized, mounted, linked, or reachable by the application runtime.

## Follow-up verification

- Run the backend and frontend quality suites on this branch.
- Verify root, login, register, `/desk`, `/admin`, scanner, approvals, settings, positions, and Guardian routes.
- Verify `/demo` and `/api/v1/demo/*` are unavailable.
- Do not submit paper orders during this cleanup.
