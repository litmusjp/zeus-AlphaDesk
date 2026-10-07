# Standalone options assessment (v2)

AlphaDesk assesses **your proposed trade**, without a watchlist, scan job, connected opportunity, caller score, or client broker credentials. The service operator supplies verified read-only market-data credentials. Existing scanner routes remain compatible.

## One request

```bash
curl "$ALPHADESK_API_URL/api/v2/option-trade-assessments" \
  -H "X-AlphaDesk-API-Key: $ALPHADESK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"legs":[{"symbol":"AAPL261120C00200000","side":"buy"}],"quantity":1,"limit_price":"2"}'
```

The symbol/price above are **format examples, not a current candidate or recommendation**. Submit your actual contract and proposed debit. Quantity is total contracts/spread units; leg `ratio_quantity` defaults to 1. `client_reference` is optional. No market/equity/Greeks declarations are accepted.

Supported initially: a long call, long put, bull-call debit vertical or bear-put debit vertical. Verticals require same underlying/expiry/type, exact 1:1 buy/sell legs and net debit below width. Standard multiplier 100 only. Naked sales, credit structures and other strategies explicitly return UNAVAILABLE/unsupported; this is not a promise of universal options coverage.

Get authenticated passing criteria with `GET /api/v2/option-trade-assessments/policy`.

## Result and passing rules

- **PASS:** genuine fresh evidence, existing catalyst/momentum score and direction, confidence/gap, quote/depth/spread/open-interest/Greeks, DTE/strike distance and proposed debit/quantity/loss caps meet the workspace policy.
- **FAIL:** sufficient evidence establishes failure. Each check includes actual value, threshold and explanation. Lower quantity may repair a loss cap, not a weak signal.
- **UNAVAILABLE:** required data/permission/freshness/strategy support or deadline is missing. Inspect `blocking_reasons`, `remediation` and `retryable`; skip this candidate rather than retrying declarations until PASS.

Fields include `signal_score` (number or null), `minimum_passing_score`, checks/remediation, exact `trade`, `trade_fingerprint`, policy version/snapshot, evidence/observation timestamps and expiry. Policy version includes the active workspace policy and the separately hashed `paper_advisory_greeks_v1` rules. This profile is server-owned: it requires actual Greek values, treats an absent independent Greek calculation time as unknown with a warning, and rejects partial, malformed, stale or future supplied provenance. It does not change execution-grade or legacy assessment rules. Missing Greek values or open interest can retain the honestly calculated underlying score while remaining unavailable.

`score_profile` is always `catalyst_momentum_v1`; `score_components` contains the weighted contributions from the existing score formula, or null when that score cannot be calculated. `evidence_as_of` identifies the market evidence timestamp used by the score. This metric is not a probability of profit. Its suitability for OpenProphet L2's mean-reversion strategy has **not** been validated.

The v2 proposal fingerprint hashes this UTF-8 canonical form (defaults are materialized and decimal text is normalized without exponent notation): `standalone-proposal-v2|q:<quantity>|p:<limit_price>|r:<reference>|n:<leg_count>|<legs>`. A missing reference is `-1:`; a present reference is its UTF-8 byte length, a colon, then its exact value. Each leg is `<symbol-byte-length>:<symbol>:<side>:<ratio>`, with omitted ratio normalized to `1`; legs retain proposal order and are separated by semicolons. The policy version is `standalone-catalyst-v2:` plus the SHA-256 of the policy snapshot serialized as sorted-key compact JSON. These hashes bind data for consistency; neither authenticates a client nor grants execution permission.

The current options snapshot exposes Greeks without an independent Greek source timestamp. The provider does not copy the fresh quote timestamp onto Greeks. Advisory outcomes expose `greeks_calculation_freshness`, `warnings`, and the actual quote times. This is research-quality paper-only feedback, never execution approval.

Scoring weights and catalyst thresholds reuse the existing framework. News uses its deterministic keyword heuristic; stock momentum/confirmations use its existing open-relative transformations (SPY/QQQ proxies). The standalone source uses prior completed bars for volume and genuine contract spreads for liquidity, not the scanner's constant liquidity placeholder. This is a quality screen, not proof of profitability or suitability for every strategy.

**Always `execution_allowed:false`.** Clients remain responsible for account identity, permissions, buying power, fees, aggregate portfolio/exposure/Greek limits, reconciliation and exit/protection responsibility. A PASS is not broker authorization.

## MCP

Install the existing optional MCP dependency, set `ALPHADESK_API_URL` to the API origin and `ALPHADESK_API_KEY` to an assessment key, then run:

```bash
uv sync --extra mcp
uv run python -m apps.mcp.server
```

Use `assess_options_trade(legs=[{"symbol":"YOUR_OCC_CONTRACT","side":"buy"}], quantity=1, limit_price="YOUR_LIMIT")`. This thin tool calls the same REST engine. Legacy `assess_options_strategy` remains for compatibility; use the new tool for standalone candidate screening.

## OpenProphet and activation boundary

The new `assess_options_trade` MCP tool calls the authenticated, account-fenced backend `/api/v1/options/trade-assessment`. Separate L1/L2 clients retain separate receipt caches. Receipts bind exact proposal (including optional reference), paper account/tenant/sandbox, API policy and expiry; concurrent identical requests coalesce. Returns are cloned so callers cannot modify cached trust. Restart safely drops the cache and requires reassessment. To reuse a planning receipt at submission, use the eventual client order ID as `client_reference`.

The new final opening guard is **inactive unless separately approved and explicitly configured as `STANDALONE_OP2`**. It retains preceding local account/buying-power/exposure/permissions/reservation/session checks, requires a verified paper identity/local policy and a durable audit sink, checks current API policy before reuse, and fails closed. It never applies to risk-reducing exits or stock trades. Current `ACCOUNT_VERIFIED` behavior remains unchanged. No activation or trading restart is part of this implementation.

Legacy MCP assessment tools remain registered for compatibility, so the literal requirement of exposing only one assessment tool is not met; a separate compatibility decision is required. The audit callback is synchronous and cannot be forcibly interrupted safely. The guard checks the five-second context after it returns, but an uncooperative callback can exceed that wall-clock budget; a cancellable/timeout-capable audit interface is a required follow-up before claiming a hard end-to-end five-second bound. Remote policy cannot be atomically locked with the subsequent broker submission.

Market reads run in parallel with two-second request timeouts, one-second evidence cache and a five-second server assessment deadline. OpenProphet's existing MCP transport has a stricter three-second budget. No synchronous LLM or paid-model fallback. Latency targets require genuine preview benchmarking; fixture timings are not market-performance evidence.
