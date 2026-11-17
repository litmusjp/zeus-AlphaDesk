from __future__ import annotations

import json

# Reuse the existing evaluator and scoring policy; this flag is never client input.
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.option_economics import (
    UNAVAILABLE,
    credit_economics,
    option_economics,
    protective_economics,
)
from packages.connected.strategy_assessment import (
    AssessmentLeg,
    StrategyAssessmentRequest,
    assess_strategy,
)
from packages.connected.trade_structure import (
    CREDIT_STRUCTURES,
    PROTECTIVE_STRUCTURES,
    classify_shape,
    gate_structure,
    price_kind,
    structure_direction,
)
from packages.domain.options import (
    LegSide,
    OptionLeg,
    OptionStructure,
    StructureType,
    UnderlyingLeg,
)
from packages.domain.workflow import Direction, NoTrade, Signal
from packages.options.engine import InvalidOptionStructure, build_structure
from packages.strategy.catalyst import CatalystMomentumStrategy, score_components, score_signal

# v2: the direction is verified against the proposed option (neutral headlines no longer force
# "bullish"); the numeric spread limit is the caller's; the receipt adds information-only blocks.
SCORE_PROFILE = "catalyst_momentum_v2"
# Protective structures are not catalyst-scored: no score, no components, and "is a hedge
# appropriate" is delegated to the caller. Unchanged OP2 cannot validate this profile (by design).
PROTECTIVE_PROFILE = "protective_structure_v1"
ADVISORY_POLICY_PROFILE = "paper_advisory_greeks_v1"
ADVISORY_POLICY_RULE = {
    "profile": ADVISORY_POLICY_PROFILE,
    "paper_only": True,
    "require_greek_values": True,
    "unknown_greek_calculation_time": "warning",
}
ADVISORY_POLICY_HASH = sha256(
    json.dumps(ADVISORY_POLICY_RULE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def proposed_direction(trade: TradeRequest) -> Direction:
    """The direction the proposal itself states, from its structure (credit puts are bullish)."""
    structure = classify_shape(trade)
    derived = structure_direction(structure) if structure is not None else None
    if derived is not None:
        return derived
    match = _OCC.fullmatch(trade.legs[0].symbol)
    return Direction.BULLISH if match is not None and match[3] == "C" else Direction.BEARISH


def resolve_direction(features: Any, proposed: Direction) -> Direction:
    """Headline sentiment decides when it has a sign; neutral headlines defer to the proposal.

    There is never a "no direction" state: the resolved direction is used in the score, the strategy
    evaluation, the confirmation text and the receipt.
    """
    if features.sentiment > 0:
        return Direction.BULLISH
    if features.sentiment < 0:
        return Direction.BEARISH
    return proposed


def _sentiment_sign(features: Any) -> str:
    if features.sentiment > 0:
        return "positive"
    return "negative" if features.sentiment < 0 else "neutral"


def _score_metadata(features: Any, resolved: Direction | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "score_profile": SCORE_PROFILE,
        "score_components": {
            name: float(value) for name, value in score_components(features, resolved).items()
        },
    }
    confidence = getattr(features, "catalyst_confidence", None)
    if confidence is not None:
        try:
            numeric = float(confidence)
        except (TypeError, ValueError, OverflowError):
            numeric = None
        if numeric is not None and 0 <= numeric <= 1:
            metadata["catalyst_confidence"] = numeric
    return metadata


class TradeLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,6}\d{6}[CP]\d{8}$", max_length=21)
    side: Literal["buy", "sell"]
    ratio_quantity: int = Field(default=1, ge=1, le=10, strict=True)


class StockContext(BaseModel):
    """Shares the caller declares it holds, for protective structures only.

    Caller-declared, NOT verified against any broker account. `entry_price` is the cost basis
    per share.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,6}$")
    shares: int = Field(ge=1, le=100_000, strict=True)
    entry_price: Decimal = Field(gt=0)


class TradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    legs: tuple[TradeLeg, ...] = Field(min_length=1, max_length=2)
    quantity: int = Field(ge=1, le=10, strict=True)
    limit_price: Decimal = Field(gt=0)
    client_reference: str | None = Field(default=None, max_length=128)
    stock_context: StockContext | None = None

    @field_validator("client_reference")
    @classmethod
    def client_reference_byte_limit(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 128:
            raise ValueError("client_reference exceeds 128 UTF-8 bytes")
        return value


def trade_wire(trade: TradeRequest) -> dict[str, Any]:
    """The `trade` echo. `stock_context` appears ONLY when it was sent: a null key would break
    OP2's strict decoder for every existing shape."""
    return trade.model_dump(mode="json", exclude=None if trade.stock_context else {"stock_context"})


def _canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def proposal_fingerprint(trade: TradeRequest) -> str:
    reference = trade.client_reference
    reference_field = (
        "-1:" if reference is None else f"{len(reference.encode('utf-8'))}:{reference}"
    )
    legs = ";".join(
        f"{len(leg.symbol)}:{leg.symbol}:{leg.side}:{leg.ratio_quantity}" for leg in trade.legs
    )
    canonical = (
        f"standalone-proposal-v2|q:{trade.quantity}|p:{_canonical_decimal(trade.limit_price)}|"
        f"r:{reference_field}|n:{len(trade.legs)}|{legs}"
    )
    stock = trade.stock_context
    if stock is not None:
        # v3 (protective structures only): the v2 string with the declared shares appended.
        canonical = canonical.replace("standalone-proposal-v2", "standalone-proposal-v3", 1)
        canonical += (
            f"|s:{len(stock.symbol)}:{stock.symbol}:{stock.shares}:"
            f"{_canonical_decimal(stock.entry_price)}"
        )
    return sha256(canonical.encode("utf-8")).hexdigest()


_PHASE_B_POLICY_KEYS = (
    "allowed_structures",
    "credit_min_credit_to_width",
    "credit_max_short_delta",
    "credit_dte_min",
    "credit_dte_max",
    "protective_max_premium_pct",
)


def policy_metadata(policy: AssessmentPolicy) -> dict[str, Any]:
    snapshot = policy.model_dump(mode="json")
    # ON is the pre-existing implicit behavior; keep its wire representation stable.
    # OFF is explicit in the snapshot and changes the effective policy fingerprint.
    if policy.require_price_action_confirmation:
        snapshot.pop("require_price_action_confirmation")
    # Phase B keys are omitted at their defaults so existing snapshots and digests do not move.
    for key in _PHASE_B_POLICY_KEYS:
        if getattr(policy, key) == AssessmentPolicy.model_fields[key].default:
            snapshot.pop(key, None)
    advisory_bytes = json.dumps(
        {"workspace_policy": snapshot, **ADVISORY_POLICY_RULE},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return {
        "scope": "TRADE_ASSESSMENT",
        "score_profile": SCORE_PROFILE,
        "policy_version": "standalone-paper-advisory-v1:"
        + sha256(advisory_bytes.encode()).hexdigest(),
        "minimum_passing_score": str(policy.minimum_signal_score),
        "supported_strategies": list(policy.allowed_structures),
        "policy_snapshot": snapshot,
        "execution_allowed": False,
        "advisory_policy_profile": ADVISORY_POLICY_PROFILE,
        "advisory_policy_hash": ADVISORY_POLICY_HASH,
        "paper_only": True,
        "human_approval_required": True,
    }


def validate_pass_receipt(
    result: Any, proposal: TradeRequest, *, now: datetime | None = None
) -> bool:
    """Validate authority-bearing fields while allowing descriptive response additions."""
    now = now or datetime.now(UTC)
    if not isinstance(result, dict):
        return False
    if any(
        result.get(key) != expected
        for key, expected in (
            ("decision", "PASS"),
            ("execution_allowed", False),
            ("scope", "TRADE_ASSESSMENT"),
            ("score_source", "alphadesk_direct_market_data"),
            ("score_profile", SCORE_PROFILE),
            ("retryable", False),
        )
    ):
        return False
    if "pass" in result and result["pass"] is not True:
        return False
    if not result.get("assessment_id"):
        return False
    try:
        score = Decimal(str(result["signal_score"]))
        threshold = Decimal(str(result["minimum_passing_score"]))
        expiry = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
        observed = datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
        evidence = datetime.fromisoformat(result["evidence_observed_at"].replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(result["evidence_as_of"].replace("Z", "+00:00"))
        components = result["score_components"]
        component_values = [Decimal(str(v)) for v in components.values()]
        snapshot_payload = result["policy_snapshot"]
        # Workspace settings were validated before the standalone managed threshold
        # was applied. Receipt validation may therefore skip only these two
        # pre-scan/execution relationships; all field and range checks still apply.
        snapshot = AssessmentPolicy.model_validate(
            snapshot_payload, context={"managed_quality_receipt": True}
        )
    except (KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False
    score_value = result.get("signal_score")
    if isinstance(score_value, bool) or not isinstance(score_value, (int, float, Decimal)):
        return False
    if isinstance(result.get("minimum_passing_score"), bool):
        return False
    if not all(t.tzinfo is not None for t in (expiry, observed, evidence, as_of)):
        return False
    if not all(v.is_finite() for v in (score, threshold, *component_values)):
        return False
    if not (Decimal(0) <= threshold <= Decimal(100) and threshold == snapshot.minimum_signal_score):
        return False
    if not (threshold <= score <= Decimal(100) and score >= Decimal(0)):
        return False
    maximum_age = timedelta(seconds=snapshot.execution_max_quote_age_seconds)
    if (
        observed > now
        or evidence > observed
        or as_of != evidence
        or expiry <= now
        or expiry > evidence + maximum_age
    ):
        return False
    limits = {
        "catalyst_confidence": Decimal("0.30"),
        "sentiment": Decimal("0.15"),
        "directional_momentum": Decimal("0.20"),
        "relative_volume": Decimal("0.10"),
        "market_confirmation": Decimal("0.075"),
        "sector_confirmation": Decimal("0.075"),
        "liquidity": Decimal("0.10"),
    }
    if not isinstance(components, dict) or set(components) != set(limits):
        return False
    if any(
        isinstance(components[name], bool)
        or not isinstance(components[name], (int, float, Decimal))
        for name in limits
    ):
        return False
    normalized_components = {name: Decimal(str(components[name])) for name in limits}
    if any(value < 0 or value > limits[name] for name, value in normalized_components.items()):
        return False
    advisory_bytes = json.dumps(
        {"workspace_policy": snapshot_payload, **ADVISORY_POLICY_RULE},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    expected_version = "standalone-paper-advisory-v1:" + sha256(advisory_bytes.encode()).hexdigest()
    if (
        result.get("policy_version") != expected_version
        or result.get("advisory_policy_profile") != ADVISORY_POLICY_PROFILE
        or result.get("advisory_policy_hash") != ADVISORY_POLICY_HASH
        or result.get("paper_only") is not True
        or result.get("human_approval_required") is not True
    ):
        return False
    market = result.get("market_data")
    if not isinstance(market, dict) or any(
        market.get(key) != expected
        for key, expected in (
            ("source", "alpaca"),
            ("feed", "indicative"),
            ("quality", "testing_only"),
        )
    ):
        return False
    greek_time, greek_source = (
        market.get("greeks_calculated_at"),
        market.get("greeks_calculation_provenance"),
    )
    if greek_time is None:
        if (
            market.get("greeks_calculation_freshness") != "unknown"
            or greek_source is not None
            or market.get("warnings") != ["greek_calculation_time_unknown"]
        ):
            return False
    else:
        try:
            parsed_greek_time = datetime.fromisoformat(greek_time.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return False
        if (
            not isinstance(greek_source, str)
            or not greek_source.strip()
            or not parsed_greek_time.tzinfo
            or parsed_greek_time > observed
            or market.get("greeks_calculation_freshness") != "verified"
            or market.get("warnings") != []
        ):
            return False
    if result.get("trade_fingerprint") != proposal_fingerprint(proposal):
        return False
    try:
        if TradeRequest.model_validate(result["trade"]) != proposal:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    checks = result.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    check_codes = [c.get("code") for c in checks if isinstance(c, dict)]
    if (
        len(check_codes) != len(checks)
        or any(not isinstance(code, str) or not code for code in check_codes)
        or len(set(check_codes)) != len(check_codes)
        or any(c.get("passed") is not True for c in checks)
    ):
        return False
    if result.get("blocking_reasons") != [] or result.get("remediation") != []:
        return False
    if abs(sum(normalized_components.values()) - score / Decimal(100)) > Decimal("0.000051"):
        return False
    return True


def _protective_metadata() -> dict[str, Any]:
    """Receipt metadata of every enabled protective assessment, healthy or not.

    No catalyst score, the protective profile and the disclosures: absent evidence must not turn
    into catalyst authority."""
    return {
        "signal_score": None,
        "score_profile": PROTECTIVE_PROFILE,
        "score_components": None,
        "minimum_passing_score": None,
        "hedge_appropriateness": "delegated_to_caller",
        "stock_context_basis": "declared by the caller; not verified against any broker account",
    }


def unavailable(
    trade: TradeRequest,
    policy: AssessmentPolicy,
    code: str = "market_data_unavailable",
    evidence: Any = None,
) -> dict[str, Any]:
    received = evidence is not None
    quote_times = (
        {c.symbol: c.quote.quoted_at.isoformat() for c in evidence.contracts} if received else None
    )
    greek_at = getattr(evidence, "greeks_at", None) if received else None
    greek_source = getattr(evidence, "greeks_source", None) if received else None
    greek_freshness = "unknown"
    greek_warnings = ["greek_calculation_time_unknown"]
    if greek_at is not None or greek_source is not None:
        # "invalid" means supplied provenance failed validity/freshness checks.
        greek_freshness = "invalid"
        greek_warnings = ["greek_calculation_time_invalid"]
        max_age = timedelta(seconds=policy.execution_max_quote_age_seconds)
        current = datetime.now(UTC)
        if (
            greek_at is not None
            and isinstance(greek_source, str)
            and greek_source.strip()
            and isinstance(greek_at, datetime)
            and greek_at.tzinfo is not None
            and greek_at <= current
            and current - greek_at < max_age
        ):
            greek_freshness = "verified"
            greek_warnings = []
    result = {
        **policy_metadata(policy),
        "assessment_id": str(uuid4()),
        "decision": "UNAVAILABLE",
        "market_data": {
            "source": "alpaca" if received else "unknown",
            "feed": getattr(evidence, "market_data_feed", "indicative") if received else None,
            "requested_feed": "indicative",
            "quality": "testing_only",
            "greeks_calculation_provenance": (greek_source),
            "greeks_calculated_at": (
                greek_at.isoformat() if isinstance(greek_at, datetime) else None
            ),
            "greeks_calculation_freshness": (greek_freshness),
            "warnings": (greek_warnings),
            "source_quote_times": quote_times,
        },
        "signal_score": None,
        "score_profile": SCORE_PROFILE,
        "score_components": None,
        "evidence_as_of": None,
        "checks": [],
        "blocking_reasons": [code],
        "remediation": ["Obtain fresh complete evidence; never alter timestamps or invent scores."],
        "retryable": True,
        "trade_fingerprint": proposal_fingerprint(trade),
        "observed_at": datetime.now(UTC).isoformat(),
        "expires_at": None,
    }
    # Only an ENABLED protective request gets the protective contract; disabled or unknown shapes
    # and every existing shape keep today's bytes.
    if gate_structure(trade, policy) in PROTECTIVE_STRUCTURES:
        result.update(_protective_metadata())
    return result


_OCC = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")


_DEFAULT_ALLOWLIST_POLICY = AssessmentPolicy()


def _spread_strategy_type(
    symbol: str,
) -> Literal["BULL_CALL_DEBIT_SPREAD", "BEAR_PUT_DEBIT_SPREAD"]:
    match = _OCC.fullmatch(symbol)
    if match is None:
        raise ValueError("unsupported option symbol")
    return "BULL_CALL_DEBIT_SPREAD" if match[3] == "C" else "BEAR_PUT_DEBIT_SPREAD"


def supported_shape(trade: TradeRequest) -> bool:
    """Legacy predicate: one of the four shapes that were assessable before the allowlist."""
    return gate_structure(trade, _DEFAULT_ALLOWLIST_POLICY) is not None


def _convert_checks(strategy_result: Any) -> list[dict[str, Any]]:
    checks = []
    for original in strategy_result.checks:
        c = original.model_dump(mode="json")
        message = c["reason"]
        if not c["passed"] and c["code"] == "policy_loss":
            message = (
                "Reduce the proposed debit or quantity until maximum planned loss is "
                "within the workspace limit."
            )
        elif not c["passed"] and c["code"] == "minimum_signal_score":
            message = (
                "The trusted market signal does not meet the workspace minimum score; "
                "wait for stronger verified evidence."
            )
        checks.append(
            {
                "code": c["code"],
                "passed": c["passed"],
                "actual": c["actual"],
                "threshold": c["limit"],
                "message": message,
            }
        )
    return checks


def _add_leg_checks(
    check: Any,
    trade: TradeRequest,
    contracts: dict[str, Any],
    policy: AssessmentPolicy,
    spot: Decimal,
    now: datetime,
) -> None:
    for leg in trade.legs:
        c = contracts[leg.symbol]
        dte = (c.expiration - now.date()).days
        check(
            f"{leg.symbol}_tradable",
            c.tradable,
            c.tradable,
            True,
            "Choose a currently tradable standard contract.",
        )
        check(
            f"{leg.symbol}_dte",
            policy.minimum_dte <= dte <= policy.maximum_dte,
            dte,
            f"{policy.minimum_dte}..{policy.maximum_dte}",
            "Choose an expiry within the permitted DTE range.",
        )
        distance = abs(c.strike - spot) / spot
        check(
            f"{leg.symbol}_strike_distance",
            distance <= policy.maximum_strike_distance_ratio,
            distance,
            policy.maximum_strike_distance_ratio,
            "Choose a strike within the permitted distance from the underlying.",
        )


def _effective_credit_dte_window(policy: AssessmentPolicy) -> tuple[int, int]:
    """The credit window intersected with the workspace window; the receipt states what was used."""
    return (
        max(policy.minimum_dte, policy.credit_dte_min),
        min(policy.maximum_dte, policy.credit_dte_max),
    )


def _add_credit_checks(
    check: Any,
    trade: TradeRequest,
    structure: StructureType,
    contracts: dict[str, Any],
    policy: AssessmentPolicy,
    spot: Decimal,
    now: datetime,
) -> None:
    short = contracts[next(leg.symbol for leg in trade.legs if leg.side == "sell")]
    long = contracts[next(leg.symbol for leg in trade.legs if leg.side == "buy")]
    width = abs(short.strike - long.strike)
    credit = trade.limit_price
    check(
        "credit_below_width",
        credit < width,
        credit,
        width,
        "The proposed net credit must be less than the spread width.",
    )
    ratio = credit / width if width > 0 else Decimal(0)
    check(
        "credit_to_width_minimum",
        ratio >= policy.credit_min_credit_to_width,
        ratio,
        policy.credit_min_credit_to_width,
        "Ask for a larger credit relative to the width, or choose a different spread.",
    )
    delta = short.quote.greeks.delta if short.quote.greeks else None
    check(
        "short_leg_delta_maximum",
        delta is not None and abs(delta) <= policy.credit_max_short_delta,
        "unavailable" if delta is None else abs(delta),
        policy.credit_max_short_delta,
        "Choose a short strike with a smaller absolute delta.",
    )
    out_of_the_money = (
        short.strike < spot
        if structure is StructureType.BULL_PUT_CREDIT_SPREAD
        else short.strike > spot
    )
    check(
        "short_strike_out_of_the_money",
        out_of_the_money,
        short.strike,
        f"{'below' if structure is StructureType.BULL_PUT_CREDIT_SPREAD else 'above'} {spot}",
        "The short strike must be out of the money.",
    )
    low, high = _effective_credit_dte_window(policy)
    dte = (short.expiration - now.date()).days
    check(
        "credit_dte_window",
        low <= dte <= high,
        dte,
        f"{low}..{high}",
        "Choose an expiry inside the credit-spread window (intersected with the workspace window).",
    )


def _structure_summary(
    trade: TradeRequest, structure: StructureType, contracts: dict[str, Any]
) -> OptionStructure | None:
    """Payoff numbers come from the engine; this only maps the request onto it."""
    sells = [leg for leg in trade.legs if leg.side == "sell"]
    buys = [leg for leg in trade.legs if leg.side == "buy"]
    limit = trade.limit_price
    entries: dict[str, Decimal] = {}
    if structure in CREDIT_STRUCTURES:
        ask = contracts[buys[0].symbol].quote.ask
        entries = {buys[0].symbol: ask, sells[0].symbol: ask + limit}
    elif sells:
        bid = contracts[sells[0].symbol].quote.bid
        entries = {buys[0].symbol: limit + bid, sells[0].symbol: bid}
    else:
        entries = {buys[0].symbol: limit}
    legs = tuple(
        OptionLeg(
            contract=contracts[leg.symbol],
            side=LegSide.LONG if leg.side == "buy" else LegSide.SHORT,
            entry_price=entries[leg.symbol],
        )
        for leg in trade.legs
    )
    stock = trade.stock_context
    underlying = (
        UnderlyingLeg(symbol=stock.symbol, shares=stock.shares, entry_price=stock.entry_price)
        if stock is not None and structure in PROTECTIVE_STRUCTURES
        else None
    )
    try:
        return build_structure(structure, legs, quantity=trade.quantity, underlying=underlying)
    except (InvalidOptionStructure, ValueError):
        return None


def _new_structure_fields(
    trade: TradeRequest,
    structure: StructureType,
    contracts: dict[str, Any],
    policy: AssessmentPolicy,
    now: datetime,
) -> dict[str, Any]:
    """Receipt fields that exist ONLY for the four newly assessable structures."""
    fields: dict[str, Any] = {
        "structure_type": structure.value,
        "price_kind": price_kind(structure),
    }
    summary = _structure_summary(trade, structure, contracts)
    if summary is not None:
        fields["max_loss"] = str(summary.max_loss)
        fields["max_profit"] = None if summary.max_profit is None else str(summary.max_profit)
        fields["break_evens"] = [str(value) for value in summary.break_evens]
        fields["position_greeks"] = {
            "unit": "per position: greek x quantity x multiplier, plus declared shares for delta",
            **{
                name: str(getattr(summary.greeks, name))
                for name in ("delta", "gamma", "theta", "vega")
            },
        }
    else:
        fields.update(max_loss=UNAVAILABLE, max_profit=UNAVAILABLE, break_evens=UNAVAILABLE)
    if structure in CREDIT_STRUCTURES:
        low, high = _effective_credit_dte_window(policy)
        fields["effective_dte_window"] = f"{low}..{high}"
        fields["iv_edge"] = "unavailable"
        fields["assignment_note"] = (
            "The short leg is American-style: it can be assigned before expiry. "
            "Assignment handling is the caller's responsibility."
        )
    elif any(leg.side == "sell" for leg in trade.legs):
        fields["assignment_note"] = (
            "The short put is American-style: it can be assigned before expiry. "
            "Assignment handling is the caller's responsibility."
        )
    return fields


def _evaluate_protective(
    trade: TradeRequest,
    policy: AssessmentPolicy,
    evidence: Any,
    now: datetime,
    structure: StructureType,
    contracts: dict[str, Any],
    times: list[datetime],
) -> dict[str, Any]:
    """Protective put / put spread against caller-declared shares.

    A separate path: it never resolves a catalyst direction, never runs the catalyst strategy and
    never applies the direct-signal, minimum-score or direction checks (or their early returns).
    """
    stock = trade.stock_context
    assert stock is not None  # protective structures are only classified with a stock context
    as_of = min(times).isoformat()
    quote_times = {symbol: c.quote.quoted_at.isoformat() for symbol, c in contracts.items()}
    fields = {**_protective_metadata(), "evidence_as_of": as_of}

    def early(code: str) -> dict[str, Any]:
        result = unavailable(trade, policy, code, evidence)
        result["market_data"]["source_quote_times"] = quote_times
        result.update(fields)
        return result

    if policy.execution_min_open_interest is not None and any(
        c.quote.open_interest is None for c in contracts.values()
    ):
        return early("open_interest_missing")
    if any(c.quote.greeks is None for c in contracts.values()):
        return early("option_greeks_missing")
    source = evidence.greeks_source
    if (evidence.greeks_at is None) != (not source) or (
        source is not None and (not isinstance(source, str) or not source.strip())
    ):
        return early("greek_provenance_contradictory")
    if any(min(c.quote.bid_size, c.quote.ask_size) <= 0 for c in contracts.values()):
        # Absent executable depth is missing evidence, not a failed structure: UNAVAILABLE.
        result = early("quote_depth")
        result["remediation"] = ["Choose genuine positive bid/ask depth meeting the minimum size."]
        return result
    expires = min(times) + timedelta(seconds=policy.execution_max_quote_age_seconds)
    greeks = {name: Decimal(0) for name in ("delta", "gamma", "theta", "vega")}
    short_bid = next(
        (contracts[item.symbol].quote.bid for item in trade.legs if item.side == "sell"), Decimal(0)
    )
    legs = []
    for leg in trade.legs:
        q = contracts[leg.symbol].quote
        legs.append(
            AssessmentLeg(
                symbol=leg.symbol,
                side=leg.side,
                quantity=trade.quantity,
                price=trade.limit_price + short_bid if leg.side == "buy" else short_bid,
                bid=q.bid,
                ask=q.ask,
                quote_size=min(q.bid_size, q.ask_size),
                quoted_at=q.quoted_at,
                open_interest=q.open_interest,
                **{n: getattr(q.greeks, n) for n in greeks},
            )
        )
        for name in greeks:
            greeks[name] += (
                getattr(q.greeks, name) * trade.quantity * (1 if leg.side == "buy" else -1)
            )
    legs.sort(key=lambda leg: leg.side != "buy")
    underlying = next(iter(contracts.values())).underlying_symbol
    summary = (
        _structure_summary(trade, structure, contracts)
        if stock.shares == trade.quantity * 100
        else None
    )
    request = StrategyAssessmentRequest(
        underlying_symbol=underlying,
        strategy_type=structure.value.upper(),
        side="buy",
        quantity=trade.quantity,
        limit_price=trade.limit_price,
        legs=tuple(legs),
        max_loss=summary.max_loss
        if summary is not None
        else trade.limit_price * trade.quantity * 100,
        greeks=greeks,
        market_evidence_at=min(times),
        observed_at=now,
        expires_at=expires,
    )
    strategy_result = assess_strategy(
        request,
        policy,
        paper_equity=None,
        broker_evidence_available=True,
        now=now,
        standalone=True,
        signal_evaluation=False,
    )
    checks = _convert_checks(strategy_result)

    def check(code: str, passed: bool, actual: Any, threshold: Any, remediation: str) -> None:
        checks.append(
            {
                "code": code,
                "passed": bool(passed),
                "actual": str(actual),
                "threshold": str(threshold),
                "message": remediation,
            }
        )

    _add_leg_checks(check, trade, contracts, policy, evidence.spot, now)
    long_put = contracts[next(leg.symbol for leg in trade.legs if leg.side == "buy")]
    check(
        "stock_symbol_matches",
        stock.symbol == underlying,
        stock.symbol,
        underlying,
        "The declared shares must be shares of the option's underlying.",
    )
    check(
        "shares_match_contracts",
        stock.shares == trade.quantity * 100,
        stock.shares,
        trade.quantity * 100,
        "Declared shares must equal contracts x 100 (the put must cover the declared shares).",
    )
    check(
        "put_floor_below_spot",
        long_put.strike <= evidence.spot,
        long_put.strike,
        f"<= {evidence.spot}",
        "The protective put strike must be at or below the current price.",
    )
    if len(trade.legs) == 2:
        short_put = contracts[next(leg.symbol for leg in trade.legs if leg.side == "sell")]
        check(
            "short_put_below_long_put",
            short_put.strike < long_put.strike,
            short_put.strike,
            f"< {long_put.strike}",
            "The short put strike must be below the long put strike.",
        )
        width = long_put.strike - short_put.strike
        check(
            "debit_below_width",
            trade.limit_price < width,
            trade.limit_price,
            width,
            "The proposed net debit must be less than the spread width.",
        )
    cost_pct = trade.limit_price * 100 / evidence.spot
    check(
        "protective_cost_maximum",
        cost_pct <= policy.protective_max_premium_pct,
        cost_pct,
        policy.protective_max_premium_pct,
        "The net premium is too large relative to the declared stock value; choose a cheaper put.",
    )
    failed = [c for c in checks if not c["passed"]]
    decision = (
        strategy_result.decision
        if strategy_result.decision == "UNAVAILABLE"
        else ("FAIL" if failed else "PASS")
    )
    receipt = {
        **unavailable(trade, policy),
        **fields,
        "decision": decision,
        "checks": checks,
        "blocking_reasons": [c["code"] for c in failed],
        "remediation": [
            f"{c['code']}: actual={c['actual']}; required={c['threshold']}. {c['message']}"
            for c in failed
        ],
        "retryable": decision == "UNAVAILABLE",
        "observed_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "evidence_observed_at": as_of,
        "market_data": {
            "source": "alpaca",
            "feed": getattr(evidence, "market_data_feed", "indicative"),
            "requested_feed": "indicative",
            "quality": getattr(evidence, "market_data_quality", "testing_only"),
            "greeks_calculation_provenance": evidence.greeks_source,
            "greeks_calculated_at": evidence.greeks_at.isoformat() if evidence.greeks_at else None,
            "greeks_calculation_freshness": "verified" if evidence.greeks_at else "unknown",
            "warnings": [] if evidence.greeks_at else ["greek_calculation_time_unknown"],
            "source_quote_times": quote_times,
        },
        "scope": "TRADE_ASSESSMENT",
        "trade": trade_wire(trade),
        "account_checks": "Delegated to caller: identity, buying power, portfolio/exposure limits, "
        "fees, reconciliation and execution authorization. The declared shares are caller-declared "
        "and not verified against any broker account. Whether a hedge is appropriate is delegated "
        "to the caller.",
        "hedge_appropriateness": "delegated_to_caller",
    }
    receipt["option_quotes"] = [
        {
            "symbol": leg.symbol,
            "side": leg.side,
            "bid": str(leg.bid),
            "ask": str(leg.ask),
            "bid_size": str(contracts[leg.symbol].quote.bid_size),
            "ask_size": str(contracts[leg.symbol].quote.ask_size),
            "quoted_at": leg.quoted_at.isoformat(),
            "spread_ratio": (
                str((leg.ask - leg.bid) / ((leg.ask + leg.bid) / Decimal("2")))
                if leg.bid + leg.ask
                else UNAVAILABLE
            ),
        }
        for leg in legs
    ]
    receipt["option_economics"] = protective_economics(
        legs=list(trade.legs),
        contracts=contracts,
        premium=trade.limit_price,
        shares=stock.shares,
        stock_entry=stock.entry_price,
        spot=evidence.spot,
        today=now.date(),
    )
    receipt.update(_new_structure_fields(trade, structure, contracts, policy, now))
    provenance = getattr(evidence, "provenance", None)
    if provenance is not None:
        receipt["evidence_provenance"] = provenance
    return receipt


def evaluate(
    trade: TradeRequest, policy: AssessmentPolicy, evidence: Any, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    structure = gate_structure(trade, policy)
    if structure is None:
        result = unavailable(trade, policy, "unsupported_strategy", evidence)
        result["retryable"] = False
        result["remediation"] = [
            "Submit a long call/put or same-expiry 1:1 directional debit vertical. "
            "Naked sales and other structures are unsupported."
        ]
        return result
    contracts = {contract.symbol: contract for contract in evidence.contracts}
    max_age = timedelta(seconds=policy.execution_max_quote_age_seconds)
    times = [evidence.features_at]
    for leg in trade.legs:
        contract = contracts.get(leg.symbol)
        if contract is None:
            return unavailable(trade, policy, "contract_evidence_missing", evidence)
        match = _OCC.fullmatch(leg.symbol)
        if match is None:
            return unavailable(trade, policy, "unsupported_strategy", evidence)
        if (
            contract.underlying_symbol != match[1]
            or contract.expiration != datetime.strptime(match[2], "%y%m%d").date()
            or contract.option_type != ("call" if match[3] == "C" else "put")
            or contract.strike != Decimal(match[4]) / 1000
            or contract.multiplier != 100
        ):
            return unavailable(trade, policy, "contract_identity_mismatch", evidence)
        times.append(contract.quote.quoted_at)
    if evidence.greeks_at is not None:
        times.append(evidence.greeks_at)
    if any(t.tzinfo is None or t > now or now - t >= max_age for t in times):
        return unavailable(trade, policy, "stale_or_invalid_evidence", evidence)
    if evidence.spot <= 0:
        return unavailable(trade, policy, "underlying_price_missing", evidence)
    if structure in PROTECTIVE_STRUCTURES:
        return _evaluate_protective(trade, policy, evidence, now, structure, contracts, times)
    proposed = structure_direction(structure)
    assert proposed is not None  # only the protective structures have no direction
    resolved = resolve_direction(evidence.features, proposed)
    if policy.execution_min_open_interest is not None and any(
        c.quote.open_interest is None for c in contracts.values()
    ):
        result = unavailable(trade, policy, "open_interest_missing", evidence)
        result["market_data"]["source_quote_times"] = {
            symbol: contract.quote.quoted_at.isoformat() for symbol, contract in contracts.items()
        }
        result["signal_score"] = float(score_signal(evidence.features, resolved))
        result.update(
            _score_metadata(evidence.features, resolved), evidence_as_of=min(times).isoformat()
        )
        return result
    if any(c.quote.greeks is None for c in contracts.values()):
        result = unavailable(trade, policy, "option_greeks_missing", evidence)
        result["market_data"]["source_quote_times"] = {
            symbol: contract.quote.quoted_at.isoformat() for symbol, contract in contracts.items()
        }
        result["signal_score"] = float(score_signal(evidence.features, resolved))
        result.update(
            _score_metadata(evidence.features, resolved), evidence_as_of=min(times).isoformat()
        )
        return result
    greeks_source = evidence.greeks_source
    if (evidence.greeks_at is None) != (not greeks_source) or (
        greeks_source is not None
        and (not isinstance(greeks_source, str) or not greeks_source.strip())
    ):
        result = unavailable(trade, policy, "greek_provenance_contradictory", evidence)
        result["market_data"]["source_quote_times"] = {
            symbol: contract.quote.quoted_at.isoformat() for symbol, contract in contracts.items()
        }
        result["signal_score"] = float(score_signal(evidence.features, resolved))
        result.update(
            _score_metadata(evidence.features, resolved), evidence_as_of=min(times).isoformat()
        )
        return result
    if any(min(c.quote.bid_size, c.quote.ask_size) <= 0 for c in contracts.values()):
        result = unavailable(trade, policy, evidence=evidence)
        result.update(
            decision="FAIL",
            signal_score=float(score_signal(evidence.features, resolved)),
            **_score_metadata(evidence.features, resolved),
            evidence_as_of=min(times).isoformat(),
            retryable=False,
            blocking_reasons=["quote_depth"],
            checks=[
                {
                    "code": "quote_depth",
                    "passed": False,
                    "actual": "0",
                    "threshold": str(policy.minimum_quote_size),
                    "message": "Choose genuine positive bid/ask depth meeting the minimum size.",
                }
            ],
            remediation=["Choose genuine positive bid/ask depth meeting the minimum size."],
        )
        return result
    expires = min(times) + max_age
    legs = []
    greeks = {name: Decimal(0) for name in ("delta", "gamma", "theta", "vega")}
    # Planned entry prices are not market evidence; genuine quotes remain unchanged.
    short_price = next(
        (contracts[item.symbol].quote.bid for item in trade.legs if item.side == "sell"), Decimal(0)
    )
    credit_shape = structure in CREDIT_STRUCTURES
    # Credit mirror of the debit construction: the long leg is bought at its ask and the short leg
    # is sold for that ask plus the net credit, so (sell price - buy price) equals the credit.
    long_ask = next(
        (contracts[item.symbol].quote.ask for item in trade.legs if item.side == "buy"), Decimal(0)
    )
    for leg in trade.legs:
        c = contracts[leg.symbol]
        q = c.quote
        g = q.greeks
        legs.append(
            AssessmentLeg(
                symbol=leg.symbol,
                side=leg.side,
                quantity=trade.quantity,
                price=(
                    (trade.limit_price + long_ask if leg.side == "sell" else long_ask)
                    if credit_shape
                    else (trade.limit_price + short_price if leg.side == "buy" else short_price)
                ),
                bid=q.bid,
                ask=q.ask,
                quote_size=min(q.bid_size, q.ask_size),
                quoted_at=q.quoted_at,
                open_interest=q.open_interest,
                **{n: getattr(g, n) if g else None for n in greeks},
            )
        )
        if g:
            for name in greeks:
                greeks[name] += getattr(g, name) * trade.quantity * (1 if leg.side == "buy" else -1)
    legs.sort(key=lambda leg: leg.side != "buy")
    underlying = next(iter(contracts.values())).underlying_symbol
    credit_width = abs(
        contracts[trade.legs[0].symbol].strike - contracts[trade.legs[-1].symbol].strike
    )
    request = StrategyAssessmentRequest(
        underlying_symbol=underlying,
        strategy_type=(
            structure.value.upper()
            if credit_shape
            else "SINGLE_LEG_OPTION"
            if len(legs) == 1
            else _spread_strategy_type(legs[0].symbol)
        ),
        side="sell" if credit_shape else "buy",
        quantity=trade.quantity,
        limit_price=trade.limit_price,
        legs=tuple(legs),
        max_loss=(
            max(Decimal(0), (credit_width - trade.limit_price) * trade.quantity * 100)
            if credit_shape
            else trade.limit_price * trade.quantity * 100
        ),
        greeks=greeks,
        market_evidence_at=min(times),
        observed_at=now,
        expires_at=expires,
    )
    score = score_signal(evidence.features, resolved)
    strategy = CatalystMomentumStrategy(
        minimum_score=policy.minimum_signal_score,
        maximum_gap=policy.maximum_gap_percent,
        minimum_catalyst_confidence=policy.minimum_catalyst_confidence,
        require_price_action_confirmation=policy.require_price_action_confirmation,
    )
    idea = strategy.evaluate_signal(
        Signal(
            symbol=underlying,
            observed_at=evidence.features_at,
            features=evidence.features,
            score=score,
            source_versions={"market": "direct-alpaca-v1"},
        ),
        resolved,
    )
    direction = None if isinstance(idea, NoTrade) else idea.direction.value
    strategy_result = assess_strategy(
        request,
        policy,
        paper_equity=None,
        broker_evidence_available=True,
        now=now,
        trusted_market_features=evidence.features,
        trusted_score_source="alphadesk_direct_market_data",
        trusted_score_observed_at=evidence.features_at,
        trusted_signal_direction=direction,
        standalone=True,
        resolved_direction=resolved,
        structure_direction=proposed.value,
    )
    checks = _convert_checks(strategy_result)

    def check(code: str, passed: bool, actual: Any, threshold: Any, remediation: str) -> None:
        checks.append(
            {
                "code": code,
                "passed": bool(passed),
                "actual": str(actual),
                "threshold": str(threshold),
                "message": remediation,
            }
        )

    if isinstance(idea, NoTrade):
        mapping = {
            "score_below_threshold": (score, policy.minimum_signal_score),
            "move_excessively_extended": (
                abs(evidence.features.gap_percent),
                policy.maximum_gap_percent,
            ),
            "price_action_not_confirming": (
                evidence.features.price_momentum,
                ">0 bullish; <0 bearish",
            ),
            "weak_catalyst_confidence": (
                evidence.features.catalyst_confidence,
                policy.minimum_catalyst_confidence,
            ),
        }
        for reason in idea.reason_codes:
            check(
                reason,
                False,
                *mapping[reason],
                "Use genuinely stronger evidence; changing quantity cannot repair a weak signal.",
            )
    price_action_confirmation = (
        "required_and_confirmed"
        if policy.require_price_action_confirmation
        and evidence.features.price_momentum
        * (Decimal("1") if resolved is Direction.BULLISH else Decimal("-1"))
        > 0
        else (
            "not_required_by_operator_policy"
            if not policy.require_price_action_confirmation
            else "required_but_not_confirmed"
        )
    )
    if not policy.require_price_action_confirmation:
        check(
            "price_action_confirmation_policy",
            True,
            "not_required_by_operator_policy",
            "operator_policy",
            "Operator policy did not require price-action confirmation; evidence is not classified "
            "as confirmed.",
        )
    _add_leg_checks(check, trade, contracts, policy, evidence.spot, now)
    if credit_shape:
        _add_credit_checks(check, trade, structure, contracts, policy, evidence.spot, now)
    elif len(legs) == 2:
        width = abs(contracts[legs[0].symbol].strike - contracts[legs[1].symbol].strike)
        check(
            "debit_below_width",
            trade.limit_price < width,
            trade.limit_price,
            width,
            "The proposed net debit must be less than the spread width.",
        )
    failed = [c for c in checks if not c["passed"]]
    decision = (
        strategy_result.decision
        if strategy_result.decision == "UNAVAILABLE"
        else ("FAIL" if failed else "PASS")
    )
    receipt = {
        **unavailable(trade, policy),
        "decision": decision,
        "signal_score": float(score),
        **_score_metadata(evidence.features, resolved),
        "evidence_as_of": min(times).isoformat(),
        "checks": checks,
        "blocking_reasons": [c["code"] for c in failed],
        "remediation": [
            f"{c['code']}: actual={c['actual']}; required={c['threshold']}. {c['message']}"
            for c in failed
        ],
        "retryable": decision == "UNAVAILABLE",
        "observed_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "evidence_observed_at": min(times).isoformat(),
        "score_source": "alphadesk_direct_market_data",
        "market_data": {
            "source": "alpaca",
            "feed": getattr(evidence, "market_data_feed", "indicative"),
            "requested_feed": "indicative",
            "quality": getattr(evidence, "market_data_quality", "testing_only"),
            "greeks_calculation_provenance": getattr(evidence, "greeks_source", None),
            "greeks_calculated_at": evidence.greeks_at.isoformat() if evidence.greeks_at else None,
            "greeks_calculation_freshness": "verified" if evidence.greeks_at else "unknown",
            "warnings": [] if evidence.greeks_at else ["greek_calculation_time_unknown"],
            "source_quote_times": {
                symbol: contract.quote.quoted_at.isoformat()
                for symbol, contract in contracts.items()
            },
        },
        "scope": "TRADE_ASSESSMENT",
        "trade": trade_wire(trade),
        "account_checks": "Delegated to caller: identity, buying power, portfolio/exposure limits, "
        "fees, reconciliation and execution authorization.",
    }
    if not policy.require_price_action_confirmation:
        receipt["price_action_confirmation"] = price_action_confirmation
    # Information only, from evidence already held; none of this changes a check, score or decision.
    receipt["direction_basis"] = {
        "proposed": proposed.value,
        "headline_sentiment": _sentiment_sign(evidence.features),
        "resolved": resolved.value,
    }
    receipt["option_quotes"] = [
        {
            "symbol": leg.symbol,
            "side": leg.side,
            "bid": str(leg.bid),
            "ask": str(leg.ask),
            "bid_size": str(contracts[leg.symbol].quote.bid_size),
            "ask_size": str(contracts[leg.symbol].quote.ask_size),
            "quoted_at": leg.quoted_at.isoformat(),
            "spread_ratio": (
                str((leg.ask - leg.bid) / ((leg.ask + leg.bid) / Decimal("2")))
                if leg.bid + leg.ask
                else UNAVAILABLE
            ),
        }
        for leg in legs
    ]
    if credit_shape:
        receipt["option_economics"] = credit_economics(
            legs=list(trade.legs),
            contracts=contracts,
            credit=trade.limit_price,
            today=now.date(),
        )
        receipt.update(_new_structure_fields(trade, structure, contracts, policy, now))
    else:
        receipt["option_economics"] = option_economics(
            legs=list(trade.legs),
            contracts=contracts,
            spot=evidence.spot,
            premium=trade.limit_price,
            today=now.date(),
            average_range_pct=getattr(evidence, "average_range_pct", None),
        )
    provenance = getattr(evidence, "provenance", None)
    if provenance is not None:
        receipt["evidence_provenance"] = provenance
    return receipt
