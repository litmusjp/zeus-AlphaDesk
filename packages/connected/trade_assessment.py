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
from packages.connected.strategy_assessment import (
    AssessmentLeg,
    StrategyAssessmentRequest,
    assess_strategy,
)
from packages.domain.workflow import NoTrade, Signal
from packages.strategy.catalyst import CatalystMomentumStrategy, score_components, score_signal

SCORE_PROFILE = "catalyst_momentum_v1"
ADVISORY_POLICY_PROFILE = "paper_advisory_greeks_v1"
ADVISORY_POLICY_RULE = {"profile": ADVISORY_POLICY_PROFILE, "paper_only": True,
                        "require_greek_values": True, "unknown_greek_calculation_time": "warning"}
ADVISORY_POLICY_HASH = sha256(json.dumps(ADVISORY_POLICY_RULE, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


def _score_metadata(features: Any) -> dict[str, Any]:
    return {
        "score_profile": SCORE_PROFILE,
        "score_components": {
            name: float(value) for name, value in score_components(features).items()
        },
    }


class TradeLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,6}\d{6}[CP]\d{8}$", max_length=21)
    side: Literal["buy", "sell"]
    ratio_quantity: int = Field(default=1, ge=1, le=10, strict=True)


class TradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    legs: tuple[TradeLeg, ...] = Field(min_length=1, max_length=2)
    quantity: int = Field(ge=1, le=10, strict=True)
    limit_price: Decimal = Field(gt=0)
    client_reference: str | None = Field(default=None, max_length=128)

    @field_validator("client_reference")
    @classmethod
    def client_reference_byte_limit(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 128:
            raise ValueError("client_reference exceeds 128 UTF-8 bytes")
        return value


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
    return sha256(canonical.encode("utf-8")).hexdigest()


def policy_metadata(policy: AssessmentPolicy) -> dict[str, Any]:
    snapshot = policy.model_dump(mode="json")
    advisory_bytes = json.dumps(
        {"workspace_policy": snapshot, **ADVISORY_POLICY_RULE}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=True
    )
    return {
        "scope": "TRADE_ASSESSMENT",
        "policy_version": "standalone-paper-advisory-v1:"
        + sha256(advisory_bytes.encode()).hexdigest(),
        "minimum_passing_score": str(policy.minimum_signal_score),
        "supported_strategies": [
            "long_call",
            "long_put",
            "bull_call_debit_spread",
            "bear_put_debit_spread",
        ],
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
        snapshot = AssessmentPolicy.model_validate(result["policy_snapshot"])
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
        "catalyst_confidence": Decimal("0.30"), "sentiment": Decimal("0.15"),
        "directional_momentum": Decimal("0.20"), "relative_volume": Decimal("0.10"),
        "market_confirmation": Decimal("0.075"), "sector_confirmation": Decimal("0.075"),
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
    advisory_bytes = json.dumps({"workspace_policy": snapshot.model_dump(mode="json"),
                                 **ADVISORY_POLICY_RULE}, sort_keys=True,
                                separators=(",", ":"), ensure_ascii=True)
    expected_version = "standalone-paper-advisory-v1:" + sha256(advisory_bytes.encode()).hexdigest()
    if (result.get("policy_version") != expected_version
            or result.get("advisory_policy_profile") != ADVISORY_POLICY_PROFILE
            or result.get("advisory_policy_hash") != ADVISORY_POLICY_HASH
            or result.get("paper_only") is not True
            or result.get("human_approval_required") is not True):
        return False
    market = result.get("market_data")
    if not isinstance(market, dict) or any(
        market.get(key) != expected
        for key, expected in (("source", "alpaca"), ("feed", "indicative"),
                              ("quality", "testing_only"))
    ):
        return False
    greek_time, greek_source = market.get("greeks_calculated_at"), market.get(
        "greeks_calculation_provenance"
    )
    if greek_time is None:
        if (market.get("greeks_calculation_freshness") != "unknown" or greek_source is not None
                or market.get("warnings") != ["greek_calculation_time_unknown"]):
            return False
    else:
        try:
            parsed_greek_time = datetime.fromisoformat(greek_time.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return False
        if (not isinstance(greek_source, str) or not greek_source.strip()
                or not parsed_greek_time.tzinfo or parsed_greek_time > observed
                or market.get("greeks_calculation_freshness") != "verified"
                or market.get("warnings") != []):
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


def unavailable(
    trade: TradeRequest, policy: AssessmentPolicy, code: str = "market_data_unavailable",
    evidence: Any = None,
) -> dict[str, Any]:
    received = evidence is not None
    quote_times = ({c.symbol: c.quote.quoted_at.isoformat() for c in evidence.contracts}
                   if received else None)
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
        if (greek_at is not None and isinstance(greek_source, str) and greek_source.strip()
                and isinstance(greek_at, datetime) and greek_at.tzinfo is not None
                and greek_at <= current and current - greek_at < max_age):
            greek_freshness = "verified"
            greek_warnings = []
    return {
        **policy_metadata(policy),
        "assessment_id": str(uuid4()),
        "decision": "UNAVAILABLE",
        "market_data": {
            "source": "alpaca" if received else "unknown",
            "feed": getattr(evidence, "market_data_feed", "indicative") if received else None,
            "requested_feed": "indicative",
            "quality": "testing_only",
            "greeks_calculation_provenance": (
                greek_source
            ),
            "greeks_calculated_at": (
                greek_at.isoformat() if isinstance(greek_at, datetime) else None
            ),
            "greeks_calculation_freshness": (
                greek_freshness
            ),
            "warnings": (
                greek_warnings
            ),
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


_OCC = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")


def supported_shape(trade: TradeRequest) -> bool:
    matches = [_OCC.fullmatch(leg.symbol) for leg in trade.legs]
    if any(leg.ratio_quantity != 1 for leg in trade.legs):
        return False
    if len(trade.legs) == 1:
        return trade.legs[0].side == "buy"
    if len({leg.symbol for leg in trade.legs}) != 2:
        return False
    if len({(m[1], m[2], m[3]) for m in matches}) != 1:
        return False
    long = next((m for leg, m in zip(trade.legs, matches, strict=True) if leg.side == "buy"), None)
    short = next(
        (m for leg, m in zip(trade.legs, matches, strict=True) if leg.side == "sell"), None
    )
    if long is None or short is None:
        return False
    return int(long[4]) < int(short[4]) if long[3] == "C" else int(long[4]) > int(short[4])


def evaluate(
    trade: TradeRequest, policy: AssessmentPolicy, evidence: Any, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if not supported_shape(trade):
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
    if policy.execution_min_open_interest is not None and any(
        c.quote.open_interest is None for c in contracts.values()
    ):
        result = unavailable(trade, policy, "open_interest_missing", evidence)
        result["market_data"]["source_quote_times"] = {
            symbol: contract.quote.quoted_at.isoformat()
            for symbol, contract in contracts.items()
        }
        result["signal_score"] = float(score_signal(evidence.features))
        result.update(_score_metadata(evidence.features), evidence_as_of=min(times).isoformat())
        return result
    if any(c.quote.greeks is None for c in contracts.values()):
        result = unavailable(trade, policy, "option_greeks_missing", evidence)
        result["market_data"]["source_quote_times"] = {
            symbol: contract.quote.quoted_at.isoformat()
            for symbol, contract in contracts.items()
        }
        result["signal_score"] = float(score_signal(evidence.features))
        result.update(_score_metadata(evidence.features), evidence_as_of=min(times).isoformat())
        return result
    greeks_source = evidence.greeks_source
    if ((evidence.greeks_at is None) != (not greeks_source)
            or (greeks_source is not None and (not isinstance(greeks_source, str)
                                               or not greeks_source.strip()))):
        result = unavailable(trade, policy, "greek_provenance_contradictory", evidence)
        result["market_data"]["source_quote_times"] = {
            symbol: contract.quote.quoted_at.isoformat()
            for symbol, contract in contracts.items()
        }
        result["signal_score"] = float(score_signal(evidence.features))
        result.update(_score_metadata(evidence.features), evidence_as_of=min(times).isoformat())
        return result
    if any(min(c.quote.bid_size, c.quote.ask_size) <= 0 for c in contracts.values()):
        result = unavailable(trade, policy, evidence=evidence)
        result.update(
            decision="FAIL",
            signal_score=float(score_signal(evidence.features)),
            **_score_metadata(evidence.features),
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
    for leg in trade.legs:
        c = contracts[leg.symbol]
        q = c.quote
        g = q.greeks
        legs.append(
            AssessmentLeg(
                symbol=leg.symbol,
                side=leg.side,
                quantity=trade.quantity,
                price=trade.limit_price + short_price if leg.side == "buy" else short_price,
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
    request = StrategyAssessmentRequest(
        underlying_symbol=underlying,
        strategy_type="SINGLE_LEG_OPTION"
        if len(legs) == 1
        else (
            "BULL_CALL_DEBIT_SPREAD"
            if _OCC.fullmatch(legs[0].symbol)[3] == "C"
            else "BEAR_PUT_DEBIT_SPREAD"
        ),
        side="buy",
        quantity=trade.quantity,
        limit_price=trade.limit_price,
        legs=tuple(legs),
        max_loss=trade.limit_price * trade.quantity * 100,
        greeks=greeks,
        market_evidence_at=min(times),
        observed_at=now,
        expires_at=expires,
    )
    score = score_signal(evidence.features)
    strategy = CatalystMomentumStrategy(
        minimum_score=policy.minimum_signal_score,
        maximum_gap=policy.maximum_gap_percent,
        minimum_catalyst_confidence=policy.minimum_catalyst_confidence,
    )
    idea = strategy.evaluate_signal(
        Signal(
            symbol=underlying,
            observed_at=evidence.features_at,
            features=evidence.features,
            score=score,
            source_versions={"market": "direct-alpaca-v1"},
        )
    )
    direction = None if isinstance(idea, NoTrade) else idea.direction.value
    result = assess_strategy(
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
    )
    checks = []
    for original in result.checks:
        c = original.model_dump(mode="json")
        checks.append(
            {
                "code": c["code"],
                "passed": c["passed"],
                "actual": c["actual"],
                "threshold": c["limit"],
                "message": c["reason"],
            }
        )

    def check(code, passed, actual, threshold, remediation):
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
        distance = abs(c.strike - evidence.spot) / evidence.spot
        check(
            f"{leg.symbol}_strike_distance",
            distance <= policy.maximum_strike_distance_ratio,
            distance,
            policy.maximum_strike_distance_ratio,
            "Choose a strike within the permitted distance from the underlying.",
        )
    if len(legs) == 2:
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
        result.decision if result.decision == "UNAVAILABLE" else ("FAIL" if failed else "PASS")
    )
    return {
        **unavailable(trade, policy),
        "decision": decision,
        "signal_score": float(score),
        **_score_metadata(evidence.features),
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
        "trade": trade.model_dump(mode="json"),
        "account_checks": "Delegated to caller: identity, buying power, portfolio/exposure limits, "
        "fees, reconciliation and execution authorization.",
    }
