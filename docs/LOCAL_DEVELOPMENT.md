# Local development and test runbook

This runbook is the canonical procedure for running and validating AlphaDesk locally.

## 1. Prerequisites

- Docker Desktop with Docker Compose v2
- `curl`
- For native checks: Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node.js, and Corepack/`pnpm`
- For Connected Paper: a hosted Supabase project configured as described in [`SUPABASE.md`](./SUPABASE.md)

All commands assume:

```bash
cd /Users/kesse/Desktop/codex/AlphaDesk
```

## 2. Create local platform configuration

Create the local file once:

```bash
cp .env.example .env.local
chmod 600 .env.local
```

Generate independent local secrets and paste the output into the matching entries:

```bash
python3 -c 'import secrets; print("v1:" + secrets.token_urlsafe(32))'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

- First output: `ALPHADESK_CREDENTIAL_MASTER_KEYS`

For Connected Paper, also configure:

- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY` using a server-only `sb_secret_...` value
- `NEXT_PUBLIC_SUPABASE_URL`
- `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- `ALPHADESK_ADMIN_EMAILS`

Keep `ALPHADESK_MODE=PAPER_ONLY` and `LLM_PROVIDER=fixture`. Connected operators supply an AI-provider key and model in the UI.

Never add Alpaca or OpenRouter operator credentials to `.env.local`. They belong in tenant-scoped **Credential Settings** and are stored encrypted in AlphaDesk PostgreSQL.

## 3. Start the application

First start or full rebuild:

```bash
docker compose --env-file .env.local up -d --build
```

Inspect readiness:

```bash
docker compose --env-file .env.local ps
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/api/v1/auth/registration-status
```

Expected:

- PostgreSQL, NATS, API, and web report healthy; the worker reports `Up`.
- `/health/ready` reports healthy configuration, database, and event bus checks.
- With complete and safe Supabase configuration, every registration-status flag is `true`.

Open:

- Web: <http://localhost:3000>
- API health: <http://localhost:8000/health/ready>
- NATS monitoring: <http://localhost:8222>

The API runs all pending Alembic migrations before serving traffic.

## 4. Restart rules

After changing Python or web code, rebuild all application images:

```bash
docker compose --env-file .env.local up -d --build --force-recreate api worker web
```

After changing only API configuration, including `SUPABASE_SECRET_KEY` or administrator emails:

```bash
docker compose --env-file .env.local up -d --force-recreate api
```

After changing worker configuration, including credential master keys or connection limits:

```bash
docker compose --env-file .env.local up -d --force-recreate worker
```

After changing `NEXT_PUBLIC_*`, rebuild rather than merely restart because Next.js embeds public values at build time:

```bash
docker compose --env-file .env.local up -d --build --force-recreate web
```

Saving/replacing operator Alpaca or OpenRouter credentials in the UI requires no restart. The API uses the new encrypted record immediately and the worker refreshes its workspace supervision loop.

## 5. Logs and database state

```bash
docker compose --env-file .env.local logs --tail=200 api
docker compose --env-file .env.local logs --tail=200 worker
docker compose --env-file .env.local logs --tail=200 web
docker compose --env-file .env.local exec api alembic current
```

The expected migration head is `20260920_0018`.

Stop without deleting persisted data:

```bash
docker compose --env-file .env.local down
```

Do not use `docker compose down -v` unless intentionally deleting the local PostgreSQL and NATS volumes.

## 6. Automated checks

Install development dependencies:

```bash
uv sync --all-groups
corepack enable
cd apps/web
pnpm install --frozen-lockfile
cd ../..
```

Start PostgreSQL and NATS before integration tests:

```bash
docker compose --env-file .env.local up -d postgres nats
```

Run Python checks from the host. The explicit localhost URLs override container hostnames that may be present in `.env.local`:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages tests
ALPHADESK_DATABASE_URL=postgresql+psycopg://alphadesk:alphadesk_dev@localhost:5432/alphadesk \
ALPHADESK_NATS_URL=nats://localhost:4222 \
uv run pytest
```

Run web checks:

```bash
cd apps/web
pnpm lint
pnpm typecheck
pnpm test
pnpm build
cd ../..
```

`make check` runs the quality suite, but when `.env.local` contains Docker-internal hostnames, use the explicit Python test command above for database-backed integration tests.

## 7. Manual acceptance walkthrough

### Administrator and invitation registration

1. Sign in using a Supabase bootstrap administrator listed in `ALPHADESK_ADMIN_EMAILS`.
2. If it has no workspace, confirm it lands on `/admin` and use **Create my Paper Workspace**. No invitation should be consumed.
3. Open **Access & Invitations**, create a one-use code, and copy its `/register?code=...` link.
4. In a private browser, register a different email using that code and sign in.
5. Verify that reusing an exhausted code fails without creating another workspace.

### Connected Paper Workspace

1. In **Credential Settings**, test and save Alpaca paper credentials and an OpenRouter key/model.
2. Confirm only masked fingerprints and verification state are returned after saving.
3. In **Market Scanner**, verify the Alpaca market clock, Eastern time, regular-session guidance, and default watchlist.
4. Click **Scan now**. Each row must show `ALPACA_REAL`; after-hours stale evidence may correctly produce `UNAVAILABLE` or `NO_TRADE`.
5. Run another scan and verify the history panel separates the two scan runs instead of accumulating one undifferentiated table.
6. Review an opportunity and run OpenRouter analysis. Provider failure must degrade only the AI panel.
7. If a fresh bounded-risk `TRADE` candidate exists, review every leg and risk check, then explicitly approve it for the next U.S. session. The worker revalidates the immutable intent, live quote, risk, account, and broker state before making one guarded Alpaca paper submission attempt.
8. Verify broker-confirmed state in **Positions & Orders** and tenant-specific controls in **Audit & Guardian**.

Conditional paper submission is a real action against the operator's Alpaca paper account and occurs only after the approved next-session checks. Do not approve merely to make a failing scan appear successful.

## 8. Troubleshooting

- **Registration unavailable:** check `/api/v1/auth/registration-status`, confirm public Supabase signup is disabled, and recreate the API after correcting server settings.
- **Admin has no workspace:** this is expected for a Dashboard-created bootstrap identity. Open `/admin` and use **Create my Paper Workspace**.
- **Scanner returns `UNAVAILABLE`:** inspect the visible market clock, quote age, data entitlements, and API/worker logs. Connected mode never substitutes fixtures.
- **No scan history:** migration `0009` intentionally did not guess groups for older opportunity rows. Run **Scan now** once to create the first explicit scan-run record.
- **AI says it needs more data:** verify the configured model and encrypted OpenRouter key. Structured-output failures are safely rejected and do not change deterministic risk or execution state.
- **Worker does not connect:** verify the Alpaca credential status is `VERIFIED`, the workspace is not suspended, and the same credential master-key version used to encrypt records is available to the worker.
