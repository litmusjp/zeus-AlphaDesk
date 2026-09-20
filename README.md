# AlphaDesk

**Bounded-risk AI options research desk with fail-closed supervisory control**

AlphaDesk is an environment-bound options research and execution desk. It defaults to PAPER; an admin may first prepare a separately credentialed LIVE target while remaining PAPER, then Enable LIVE only after fresh readiness gates, explicit live confirmation, and a separate first-live-order confirmation pass. The persisted LIVE worker must independently establish its trade-updates stream and reconciliation before execution. Quantitative risk rules and options math govern decisions; AI is a read-only qualitative analyst with no broker or execution authority.

## Connected Paper Workspace

AlphaDesk is developed and evaluated through one authenticated, invite-controlled workspace:

- Supabase-authenticated operator identity;
- server-derived, tenant-isolated workspace ownership;
- encrypted operator-owned Alpaca Paper and AI-provider credentials;
- real-source market and options evidence with no synthetic fallback;
- explicit human confirmation for every order, plus a separate exact-intent confirmation before the first live order;
- deterministic risk, Guardian, freshness, idempotency, and broker-reconciliation gates.

Existing workspaces, approvals, and orders remain PAPER-bound. No paper approval is reinterpreted as LIVE, and legacy `ALPACA` credential records are not valid for execution.

## Core controls

1. **Deterministic pipeline:** `Quant Signal → Trade Idea → Structure Candidate → Risk Decision → Order Intent`.
2. **Defined-risk structures:** strict max-loss, break-even, liquidity, and Greeks validation.
3. **Advisory AI:** cited, schema-validated analysis with no tools, credentials, pricing authority, or execution access.
4. **Guardian supervision:** fail-closed broker-state monitoring and manual halt/recovery controls.
5. **Tenant-bound vault:** Alpaca Paper and AI-provider secrets are write-only and encrypted with tenant-bound AES-256-GCM.
6. **Human authorization:** execution requires immutable review, acknowledgement, fresh preflight, and explicit submission.

## Quick start

Requirements: Docker Desktop with Docker Compose v2. Native checks additionally require Python 3.12, `uv`, Node.js, and `pnpm` through Corepack.

```bash
cd /path/to/AlphaDesk
cp .env.example .env.local
docker compose --env-file .env.local up -d --build
```

Complete platform values in `.env.local`. Do not put operator Alpaca or AI-provider credentials there; operators enter them in **Connected Paper → Credential Settings**.

Open <http://localhost:3000>, register with a server-issued invitation, and sign in. Public Supabase signup remains disabled.

## Common commands

```bash
# Follow application logs
docker compose --env-file .env.local logs -f api worker web

# Rebuild application containers after code changes
docker compose --env-file .env.local up -d --build --force-recreate api worker web

# Stop the stack while preserving PostgreSQL and NATS volumes
docker compose --env-file .env.local down

# Run all native checks
make check
```

Changing `NEXT_PUBLIC_*` values requires rebuilding `web`. Changing API or worker environment values requires recreating the affected container. Saving tenant credentials in the UI does not require a restart; the worker discovers verified Alpaca credentials automatically.

## Security boundaries

- Every new workspace starts in persisted `PAPER`. Runtime selection uses the persisted environment and its matching `ALPACA_PAPER` or `ALPACA_LIVE` credential identity; missing or mismatched readiness fails closed.
- The Admin Console is the only mode-control surface. Mode changes are audited (including rejected attempts), blocked while orders, approvals, leases, ambiguous submissions, or positions exist, and require healthy Guardian, fresh reconciliation, a connected trade-update stream, and a healthy target account.
- All operator routes require a verified Supabase JWT and derive workspace ownership server-side.
- Supabase public signup is disabled; AlphaDesk validates invitations before creating identities through server-only Admin Auth.
- Alpaca and AI-provider secrets are write-only, encrypted with tenant-bound AES-256-GCM, and never returned to the browser.
- The worker is explicitly denied `SUPABASE_SECRET_KEY`.
- Connected scans use provider provenance or return unavailable/`NO_TRADE`; synthetic fallback is prohibited.
- Only the tenant execution engine can submit an order, after explicit confirmation and fresh risk, Guardian, broker-state, account, quote, and idempotency checks.

## Documentation

- [Local development and testing](./docs/LOCAL_DEVELOPMENT.md)
- [Current project status and resumption guide](./docs/PROJECT_STATUS.md)
- [Architecture and trust boundaries](./docs/architecture.md)
- [Hosted Supabase setup](./docs/SUPABASE.md)
- [Connected Paper walkthrough](./docs/DEMO.md)
- [Acceptance record](./docs/ACCEPTANCE.md)
- [Lightsail deployment and restore](./docs/LIGHTSAIL.md)
