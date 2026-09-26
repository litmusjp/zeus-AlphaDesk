# AlphaDesk Handover Brief

**Prepared:** September 26, 2026

**Purpose:** Give a new developer/model enough context to continue AlphaDesk safely without relying on prior chat history.

## 1. Executive summary

AlphaDesk is a connected, authenticated, tenant-isolated options-trading desk built around Alpaca. The current supported workflow is the **Connected Paper Workspace**: an invited operator signs in, scans real market data, reviews an immutable order intent, explicitly pre-approves a bounded-risk trade, and a worker revalidates all safety and broker-readiness gates before any paper submission.

The current workstream focused on conditional/pre-approved trades and time-based exits. The most important recent correction is that AlphaDesk now recommends a valid exchange-session exit instead of leaving the time-based exit blank or coupling it to recommendation expiry. Invalid legacy exit plans are quarantined rather than silently changed.

**Current release posture:** isolated Railway preview only, paper-only, not production-ready for live trading. Do not enable live trading or submit real-money orders.

## 2. Source repository and current code state

- Local workspace: `C:/Users/jtoed/AlphaDesk-review`
- Git remote: `https://github.com/litmusjp/AlphaDesk.git`
- GitHub canonical repository: `https://github.com/litmusjp/zeus-AlphaDesk`
- Current branch: `fix/session-approval-expiry`
- Latest commit: `e0ad1e1 fix(ui): recommend session-valid time exit`
- Branch was clean and synchronized with origin at the last verification.
- Relevant earlier commits:
  - `208a891 fix(trading): quarantine invalid legacy exit plans`
  - `ef293a9 fix(trading): validate preapproval exit sessions`

When continuing work, inspect the current branch and diff before changing anything. Preserve unrelated work and do not rewrite history.

## 3. Railway deployment

The isolated preview is in Railway project `743ea059-5c30-4950-ab91-ee55b78a7f79`, environment `01b038bb-df33-4025-8cd9-6ba8043e0bd6` (`production` environment name inside the isolated project).

### Preview services

- Web service: `33fddcc9-1ac6-40be-a9f0-e693673da889`
  - URL: `https://web-production-1aedb.up.railway.app/`
  - Latest known deployment: `1b02c983-fb9e-4709-a1db-a4247f3564c9`
- API service: `680fab4c-9d6f-43ae-9d33-20689c66b6dc`
  - URL: `https://api-production-4689.up.railway.app/`
  - Latest e0ad1e1 deployment was reported running; re-check Railway status before relying on the exact deployment ID.
- Worker service: `fed5d832-1d69-407e-9c10-de1aa9f9c181`
- NATS and PostgreSQL are also present in the isolated Railway environment.

Railway deployments must remain isolated and paper-only. After a code change: run focused tests, commit and push to GitHub, deploy the affected Railway service(s), wait for `SUCCESS`/`RUNNING`, inspect logs, and verify the actual UI/API behavior. A successful Railway deployment alone is not sufficient evidence.

## 4. Latest architecture

```text
Authenticated operator
        |
        v
Supabase Auth/JWT ---> API derives authenticated workspace/tenant
        |
        +--> Next.js Connected Paper UI
        |      - scanner
        |      - order review / pre-approval
        |      - active approvals
        |      - positions, settings, Guardian, admin
        |
        +--> FastAPI API
        |      - authenticated desk routes
        |      - opportunities and scan history
        |      - conditional approvals
        |      - projections and diagnostics
        |
        +--> PostgreSQL
        |      - tenant/workspace state
        |      - credentials metadata and encrypted secrets
        |      - opportunities, immutable intents, approvals, audit state
        |
        +--> NATS JetStream
               - workspace-namespaced events/outbox

Worker / execution engine
        |
        +--> broker connection supervisor
        +--> Alpaca paper market clock/calendar
        +--> market data and broker projections
        +--> conditional approval revalidation
        +--> Guardian/risk/reconciliation/final-submission gates
        +--> Alpaca Paper API only
```

### Important architecture and safety boundaries

- Supabase JWT authentication is required for connected operator workflows.
- The server derives the workspace from the authenticated identity; browser-supplied tenant IDs are not trusted.
- Alpaca paper and live credentials are separate, encrypted, tenant-bound, and write-only in API responses.
- Only the execution engine may submit orders.
- PAPER is the default and current preview mode. LIVE is fail-closed behind separate admin/readiness controls and must not be enabled in this handoff work.
- Orders are bound to environment, account, credentials, immutable approval, stable client-order ID, Guardian state, reconciliation, and final submission-fencing checks.
- Broker acknowledgement is not a fill. A fill must be confirmed through reconciliation.
- No synthetic quotes, fallback contracts, silent trade substitutions, or silent changes to approved intent are allowed.

## 5. Latest features and fixes

### Exchange-session-aware time-based exits

- Planned exits are validated against the authoritative exchange calendar and timezone, not the operator's local weekday.
- A Saturday in Japan may represent a valid Friday U.S. exchange session; local calendar date alone is not a rejection criterion.
- Exit timestamps must be timezone-aware and fall inside a valid regular exchange session.
- The UI no longer uses recommendation expiry as the planned exit.
- The UI now requests/recommends the next eligible session's exit, currently designed as a time before that session's close, and pre-fills the field for operator review.
- If calendar data is unavailable, AlphaDesk must show an explicit unavailable/error state rather than inventing a timestamp.
- Manual changes remain possible but must pass the same validation.

### Invalid legacy-plan quarantine

- Existing approvals with invalid legacy planned exits are not silently corrected, rolled to Monday, or converted into another trade.
- They are transitioned to `CONDITION_FAILED` with structured reasons such as:
  - `exit_plan_expiry_before_session_open`
  - `exit_plan_expiry_not_regular_session`
- A renewed approval is required after quarantine.

### Conditional approval lifecycle

Keep these values distinct and persisted/displayed separately:

1. recommendation validity/expiry;
2. approved entry session;
3. entry authorization expiry;
4. planned position exit deadline.

Closed-market approvals should remain durable and retryable for the authoritative next eligible session. Open-market processing must fail closed and persist structured failure outcomes when any gate fails.

### Existing connected-workspace capabilities

The branch also contains authenticated workspaces, invitation-gated registration, admin console, tenant-scoped credentials and projections, real Alpaca-backed scanner evidence, bounded-risk option structures, AI-provider BYOK integration, Guardian controls, immutable review, explicit acknowledgement, deterministic preflight, idempotent client order IDs, reconciliation, and connected UI routes.

## 6. Key files to start with

- `packages/connected/market_clock.py` — exchange calendar, broker clock, timezone/session rules.
- `packages/connected/opportunities.py` — recommendation generation, expiry, approved-intent refresh.
- `packages/execution/conditional_approval.py` — approval validation, revalidation, quarantine, structured reasons.
- `packages/execution/conditional_runner.py` — conditional execution orchestration and retry/terminal outcomes.
- `packages/execution/conditional_exit_runner.py` — time-based exit validation and processing.
- `packages/execution/conditional_store.py` — approval persistence and state transitions.
- `packages/execution/connected_paper.py` — paper execution and final pre-submission gates.
- `packages/broker/alpaca_adapter.py` — broker client/order construction and provider errors.
- `apps/api/routes/desk.py` — approval and desk API responses, exit fields, diagnostics.
- `apps/web/src/components/order-review.tsx` — planned-exit recommendation and review UI.
- `apps/web/src/components/conditional-approvals.tsx` — approval states and diagnostic display.
- `apps/worker/__main__.py` — worker scheduling; known unrelated formatting issue may remain.
- `tests/unit/test_session_calendar.py`
- `tests/unit/test_conditional_approval.py`
- `tests/unit/test_conditional_exit_runner.py`
- `tests/unit/test_conditional_store.py`
- `tests/unit/test_approved_intent_refresh.py`
- `tests/api/test_health.py`
- `docs/architecture.md`, `docs/PROJECT_STATUS.md`, `docs/ACCEPTANCE.md`

## 7. Verification already completed

Focused backend tests for session calendar, approval, exit runner, store, approved-intent refresh, and desk exit recommendation passed after the latest fixes. Frontend order-review and conditional-approval tests passed. TypeScript, ESLint, Ruff, formatting, and `git diff --check` passed for the latest change.

Authenticated preview verification previously confirmed that invalid legacy approvals were shown as `CONDITION_FAILED` with structured reasons and that the preview remained `PAPER_ONLY` with no broker order submitted.

At the latest authenticated check, three approvals had been pre-approved in the preview:

- TLT — 9 contracts
- SPY — 3 contracts
- INTC — 3 contracts

They were observed as `APPROVED_FOR_SESSION`, bound to the next U.S. session, and awaiting next-session revalidation. This is an observation, not proof of successful worker execution or fill.

## 8. Current key issues and unfinished acceptance work

These are the main items a new developer should investigate next:

1. **Complete fresh end-to-end acceptance.** Create/observe a fresh approval through UI → API → persistence → worker and verify the exact persisted timestamps and state transitions.
2. **Verify the recommended exit is actually consumed by the worker.** UI prefill is fixed, but acceptance must prove the same planned exit survives reload/API persistence and is used during processing.
3. **Verify the Japan-local Saturday case.** Confirm a valid U.S. Friday regular session is accepted when displayed as Saturday in Japan, while genuinely closed sessions are rejected/deferred.
4. **Verify durable closed-market retry.** Confirm `market_session_closed` is retryable, remains durable, and advances only to the authoritative next eligible session.
5. **Verify open-market structured failures.** Exercise quote, bounds, liquidity, risk, Guardian, account, reconciliation, contract identity, expiry, and broker-readiness failures; confirm each is persisted with a useful stage/reason and no order is submitted.
6. **Reconcile the three pre-approved trades.** On the next eligible session, inspect worker logs and API state. Distinguish attempted/submitted/acknowledged/reconciled fill states; never report acknowledgement as a fill.
7. **Do not perform an uncontrolled paper submission.** A controlled paper-only submission is optional and requires explicit scope; it must be reconciled afterward.
8. **Separate unrelated CI baseline failures.** Earlier CI evidence included formatting in `apps/worker/__main__.py` and an Alembic migration upgrade conflict involving `broker_positions.identity_validated_at`. Diagnose/fix separately from exit-timing work.
9. **Check deployment/source consistency.** Railway previously had a web deployment-root issue when uploading from the repository root versus `apps/web`; verify future deployments build from the intended service context and do not accidentally replace the web service with the API image.
10. **Do not treat preview health as release readiness.** Keep production and LIVE disabled until authenticated acceptance and the state/reconciliation evidence above are complete.

## 9. Standing instructions from the user

- **Codex owns all coding work.** Hermes scopes the task, supplies a bounded specification, reviews the diff, runs verification, and manages GitHub/Railway.
- For coding, use **Codex CLI pinned to `gpt-5.6-luna` with `model_reasoning_effort=medium`**. Do not silently substitute another coding agent/model.
- Preserve existing user work and make the smallest coherent change.
- Back up code to GitHub, then deploy the isolated preview to Railway for the user to review/check. Do not deploy production unless explicitly instructed.
- Keep the preview paper-only. Never enable live trading, real-money trading, or unauthorized broker activity.
- Never expose, copy, or preserve credentials, tokens, API keys, passwords, or connection strings. Use `[REDACTED]` in notes.
- Preserve exact approved intent. Never fabricate quotes, silently select a different contract, create fallback trades, convert a rejection into another trade, or silently change an approved exit.
- Keep approval, risk, Guardian, broker-account, market-data, reconciliation, contract-identity, expiry, and final-submission gates intact.
- Verify external state after mutations: GitHub commit/branch, Railway deployment status/logs, API/UI state, and broker reconciliation where applicable.
- The user is non-technical and prefers autonomous low/medium-risk execution, concise status reports, and clear remaining blockers.

## 10. Recommended first session for the new developer

1. Read this file plus `docs/architecture.md`, `docs/PROJECT_STATUS.md`, and `docs/ACCEPTANCE.md`.
2. Load the relevant Codex, trading-system-change-review, systematic-debugging, GitHub, and Railway skills.
3. Inspect `git status`, branch, remote, latest commit, and current Railway status before editing.
4. Review the latest conditional-approval and exit-recommendation tests and run them unchanged.
5. Inspect the three current approval records through the authenticated preview/API without submitting anything.
6. Define a short acceptance matrix for the five unfinished scenarios in section 8.
7. Use Codex for any implementation/test edits, then review the actual diff yourself.
8. Commit and push to GitHub, deploy only the affected isolated Railway services, and verify runtime behavior before reporting completion.

## 11. Hard prohibitions

Do not deploy production, enable LIVE, submit real orders, rotate secrets, delete persistent state, rewrite repository history, or silently modify/roll forward a user's approved trade. When uncertain about a stateful or externally impactful action, stop and inspect rather than guessing.
