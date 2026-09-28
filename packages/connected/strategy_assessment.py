from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.connected.assessment_policy import AssessmentPolicy
from packages.domain.workflow import CatalystFeatures
from packages.strategy.catalyst import score_signal


class AssessmentLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    quantity: int = Field(ge=1, le=10)
    price: Decimal = Field(ge=0)
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    quote_size: Decimal = Field(gt=0)
    quoted_at: datetime
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None

    @field_validator("quoted_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("quoted_at must be timezone-aware")
        return value


class StrategyAssessmentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying_symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.]+$")
    strategy_type: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    quantity: int = Field(ge=1, le=10)
    limit_price: Decimal = Field(gt=0)
    legs: tuple[AssessmentLeg, ...] = Field(min_length=1, max_length=4)
    max_loss: Decimal = Field(ge=0)
    greeks: dict[str, Decimal]
    market_scanner_features: CatalystFeatures | None = None
    market_evidence_at: datetime
    observed_at: datetime
    expires_at: datetime
    scope: Literal["ACCOUNT_VERIFIED", "SIGNAL_QUALITY"] = "ACCOUNT_VERIFIED"
    external_account_id: str | None = Field(default=None, min_length=1, max_length=128)
    external_sandbox_id: str | None = Field(default=None, min_length=1, max_length=128)
    external_environment: Literal["PAPER"] | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> StrategyAssessmentRequest:
        timestamps = (self.market_evidence_at, self.observed_at, self.expires_at)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("assessment timestamps must be timezone-aware")
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        if self.is_external and (
            not self.external_account_id
            or not self.external_sandbox_id
            or self.external_environment != "PAPER"
        ):
            raise ValueError("external assessments require an exact PAPER account and sandbox")
        if self.scope == "SIGNAL_QUALITY" and not self.is_external:
            raise ValueError("SIGNAL_QUALITY requires a declared PAPER account and sandbox")
        return self

    @property
    def is_external(self) -> bool:
        return any(
            value is not None
            for value in (
                self.external_account_id,
                self.external_sandbox_id,
                self.external_environment,
            )
        )


class AssessmentCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    passed: bool
    actual: Any = None
    limit: Any = None
    reason: str


class StrategyAssessmentResult(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    pass_: bool = Field(alias="pass")
    decision: Literal["PASS", "FAIL", "UNAVAILABLE"]
    assessment_id: UUID = Field(default_factory=uuid4)
    strategy_identity: dict[str, Any]
    scope: Literal["ACCOUNT_VERIFIED", "SIGNAL_QUALITY"]
    market_scanner_signal_score: Decimal | None = None
    checks: tuple[AssessmentCheck, ...]
    failed_check_codes: tuple[str, ...]
    policy_snapshot: dict[str, Any]
    policy_version: str
    policy_updated_at: datetime | None = None
    market_evidence_at: datetime
    observed_at: datetime
    expires_at: datetime
    paper_only: Literal[True] = True
    human_approval_required: Literal[True] = True
    execution_allowed: Literal[False] = False
    external_identity: dict[str, str] | None = None
    score_source: str | None = None
    score_observed_at: datetime | None = None
    option_evidence_provenance: Literal["CALLER_SUPPLIED"] = "CALLER_SUPPLIED"


def _check(code: str, passed: bool, actual: Any, limit: Any, reason: str) -> AssessmentCheck:
    return AssessmentCheck(code=code, passed=passed, actual=actual, limit=limit, reason=reason)


_OCC_SYMBOL = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


def _option_shape_check(request: StrategyAssessmentRequest) -> tuple[bool, str]:
    if request.scope != "SIGNAL_QUALITY":
        return (
            len(request.legs) in (2, 4),
            "Supported option strategy shape."
            if len(request.legs) in (2, 4)
            else "Supported option strategy shape requires two or four legs.",
        )
    parsed: list[tuple[str, str, int]] = []
    for leg in request.legs:
        match = _OCC_SYMBOL.fullmatch(leg.symbol.upper())
        if match is None:
            return False, "Every option leg must use an unambiguous OCC identity."
        if match.group("root") != request.underlying_symbol.upper():
            return False, "Every option leg must match the requested underlying."
        parsed.append((match.group("date"), match.group("type"), int(match.group("strike"))))
    if len(parsed) == 1:
        strategy_type = request.strategy_type.upper()
        strategy_matches_type = (
            strategy_type not in {"BULL_CALL", "BEAR_PUT"}
            or (strategy_type == "BULL_CALL" and parsed[0][1] == "C")
            or (strategy_type == "BEAR_PUT" and parsed[0][1] == "P")
        )
        return (
            strategy_type in {"SINGLE_LEG_OPTION", "BULL_CALL", "BEAR_PUT"}
            and strategy_matches_type
            and request.side == "buy"
            and request.legs[0].side == "buy",
            "Only a long single-leg call or put is supported.",
        )
    if len(parsed) != 2 or request.side != "buy":
        return False, "Only a two-leg debit vertical is supported beyond a single long option."
    (date_a, type_a, _), (date_b, type_b, _) = parsed
    if date_a != date_b or type_a != type_b:
        return False, "A debit vertical requires matching expiry and option type."
    if {request.legs[0].side, request.legs[1].side} != {"buy", "sell"}:
        return False, "A debit vertical requires one long and one short leg."
    long_index = 0 if request.legs[0].side == "buy" else 1
    long_strike, short_strike = parsed[long_index][2], parsed[1 - long_index][2]
    if type_a == "C" and long_strike >= short_strike:
        return False, "A call debit vertical requires the long strike below the short strike."
    if type_a == "P" and long_strike <= short_strike:
        return False, "A put debit vertical requires the long strike above the short strike."
    strategy_type = request.strategy_type.upper()
    strategy_matches_type = (
        strategy_type != "BULL_CALL_DEBIT_SPREAD" or type_a == "C"
    ) and (strategy_type != "BEAR_PUT_DEBIT_SPREAD" or type_a == "P")
    return strategy_matches_type and strategy_type in {
        "BULL_CALL_DEBIT_SPREAD", "BEAR_PUT_DEBIT_SPREAD", "DEBIT_VERTICAL",
    }, "Only a supported two-leg debit vertical is accepted."


def assess_strategy(
    request: StrategyAssessmentRequest,
    policy: AssessmentPolicy,
    *,
    paper_equity: Decimal | None = None,
    broker_evidence_available: bool = True,
    now: datetime | None = None,
    policy_updated_at: datetime | None = None,
    trusted_market_features: CatalystFeatures | None = None,
    trusted_score_source: str | None = None,
    trusted_score_observed_at: datetime | None = None,
    trusted_signal_direction: str | None = None,
) -> StrategyAssessmentResult:
    current = now or datetime.now(UTC)
    age = Decimal(str((current - request.market_evidence_at).total_seconds()))
    checks: list[AssessmentCheck] = []
    shape_passed, shape_reason = _option_shape_check(request)
    checks.append(
        _check(
            "strategy_shape",
            shape_passed,
            len(request.legs),
            "single long option or two-leg debit vertical",
            shape_reason,
        )
    )
    checks.append(
        _check(
            "policy_loss",
            request.max_loss <= policy.max_investment_per_candidate,
            request.max_loss,
            policy.max_investment_per_candidate,
            "Maximum loss is within the workspace Candidate Assessment policy.",
        )
    )
    checks.append(
        _check(
            "policy_quantity",
            request.quantity <= policy.maximum_contracts_per_candidate,
            request.quantity,
            policy.maximum_contracts_per_candidate,
            "Quantity is within the workspace policy cap.",
        )
    )
    signal_quality = request.scope == "SIGNAL_QUALITY"
    evidence_code = (
        "signal_account_declaration"
        if signal_quality
        else ("external_account_evidence" if request.is_external else "broker_evidence")
    )
    checks.append(
        _check(
            evidence_code,
            True if signal_quality else broker_evidence_available,
            True if signal_quality else broker_evidence_available,
            True,
            (
                "Declared PAPER account and sandbox are client declarations; "
                "AlphaDesk did not independently verify them."
                if signal_quality
                else "Fresh independently established evidence for the requested "
                "external paper account is required."
                if request.is_external
                else "Fresh reconciled paper account evidence is required."
            ),
        )
    )
    checks.append(
        _check(
            "evidence_fresh",
            Decimal(0) <= age <= policy.execution_max_quote_age_seconds
            and current < request.expires_at,
            age,
            policy.execution_max_quote_age_seconds,
            "Market evidence must be current and unexpired.",
        )
    )
    for index, leg in enumerate(request.legs):
        if signal_quality:
            quote_age = Decimal(str((current - leg.quoted_at).total_seconds()))
            checks.append(
                _check(
                    f"leg_{index}_quote_fresh",
                    current >= leg.quoted_at
                    and quote_age <= policy.execution_max_quote_age_seconds,
                    quote_age,
                    policy.execution_max_quote_age_seconds,
                    "Each option quote timestamp must be current and not future-dated.",
                )
            )
        spread = (
            (leg.ask - leg.bid) / ((leg.ask + leg.bid) / Decimal("2"))
            if leg.bid + leg.ask
            else Decimal("999")
        )
        checks.append(
            _check(
                f"leg_{index}_quote",
                leg.ask >= leg.bid and spread <= policy.execution_max_spread_ratio,
                spread,
                policy.execution_max_spread_ratio,
                "Caller-supplied quote evidence must be usable; AlphaDesk does not "
                "independently verify it.",
            )
        )
        checks.append(
            _check(
                f"leg_{index}_liquidity",
                leg.open_interest is not None
                and leg.open_interest >= policy.execution_min_open_interest
                and leg.quote_size >= policy.minimum_quote_size,
                {"open_interest": leg.open_interest, "quote_size": leg.quote_size},
                {
                    "open_interest": policy.execution_min_open_interest,
                    "quote_size": policy.minimum_quote_size,
                },
                "Each leg must meet execution liquidity limits.",
            )
        )
        checks.append(
            _check(
                f"leg_{index}_greeks",
                not policy.require_greeks
                or all(
                    getattr(leg, name) is not None for name in ("delta", "gamma", "theta", "vega")
                ),
                True,
                True,
                "Greeks are required for deterministic portfolio risk.",
            )
        )
    checks.append(
        _check(
            "portfolio_greeks",
            all(name in request.greeks for name in ("delta", "gamma", "theta", "vega")),
            request.greeks,
            "delta/gamma/theta/vega",
            "Strategy Greeks must be complete.",
        )
    )
    if signal_quality:
        pass
    elif request.is_external and (paper_equity is None or paper_equity <= 0):
        checks.append(
            _check(
                "external_equity_evidence",
                False,
                paper_equity,
                "> 0",
                "Independent external paper equity is unavailable.",
            )
        )
    elif paper_equity is None or paper_equity <= 0:
        checks.append(
            _check(
                "paper_equity", False, paper_equity, "> 0", "Current paper equity is unavailable."
            )
        )
    else:
        loss_pct = request.max_loss / paper_equity * 100
        checks.append(
            _check(
                "risk_per_trade",
                loss_pct <= policy.max_planned_loss_per_trade_pct_equity,
                loss_pct,
                policy.max_planned_loss_per_trade_pct_equity,
                "Planned loss is within the policy percentage of paper equity.",
            )
        )
    score_features = (
        trusted_market_features if request.is_external else request.market_scanner_features
    )
    score_source = trusted_score_source if request.is_external else (
        "caller_market_scanner_features" if request.market_scanner_features is not None else None
    )
    score_observed_at = trusted_score_observed_at if request.is_external else (
        request.market_evidence_at if request.market_scanner_features is not None else None
    )
    calculated_signal_score = (
        score_signal(score_features) if score_features is not None else None
    )
    market_scanner_signal_score = calculated_signal_score
    if request.is_external:
        score_ready = (
            calculated_signal_score is not None
            and score_source == "alphadesk_connected_opportunity"
            and (signal_quality or broker_evidence_available)
            and (signal_quality or (paper_equity is not None and paper_equity > 0))
            and score_observed_at is not None
            and current >= score_observed_at
            and current - score_observed_at <= timedelta(
                seconds=policy.execution_max_quote_age_seconds
            )
        )
        checks.append(
            _check(
                "market_scanner_score_evidence",
                score_ready,
                calculated_signal_score if score_ready else None,
                policy.minimum_signal_score,
                "A fresh AlphaDesk-owned market signal is required for external assessment.",
            )
        )
        if calculated_signal_score is not None and score_ready:
            checks.append(
                _check(
                    "minimum_signal_score",
                    calculated_signal_score >= policy.minimum_signal_score,
                    calculated_signal_score,
                    policy.minimum_signal_score,
                    "The trusted market signal meets the workspace minimum score.",
                )
            )
        if not score_ready:
            market_scanner_signal_score = None
            score_source = None
            score_observed_at = None
        if signal_quality and trusted_signal_direction is not None:
            option_match = _OCC_SYMBOL.fullmatch(request.legs[0].symbol.upper())
            option_direction = (
                "BULLISH" if option_match is not None and option_match.group("type") == "C"
                else "BEARISH"
            )
            checks.append(
                _check(
                    "signal_direction",
                    shape_passed and trusted_signal_direction == option_direction,
                    trusted_signal_direction,
                    option_direction,
                    "The proposed option direction must match the AlphaDesk signal direction.",
                )
            )
    failed = tuple(check.code for check in checks if not check.passed)
    decision: Literal["PASS", "FAIL", "UNAVAILABLE"] = (
        "PASS"
        if not failed
        else (
            "UNAVAILABLE"
            if any(
                code in {
                    "external_account_evidence",
                    "external_equity_evidence",
                    "broker_evidence",
                    "paper_equity",
                    "evidence_fresh",
                    "market_scanner_score_evidence",
                }
                or code.endswith("_quote_fresh")
                for code in failed
            )
            else "FAIL"
        )
    )
    return StrategyAssessmentResult(
        pass_=not failed,
        decision=decision,
        strategy_identity={
            "underlying_symbol": request.underlying_symbol.upper(),
            "strategy_type": request.strategy_type,
            "side": request.side,
            "quantity": request.quantity,
        },
        scope=request.scope,
        market_scanner_signal_score=market_scanner_signal_score,
        checks=tuple(checks),
        failed_check_codes=failed,
        policy_snapshot=policy.model_dump(mode="json"),
        policy_version="candidate-assessment-v1",
        policy_updated_at=policy_updated_at,
        market_evidence_at=request.market_evidence_at,
        observed_at=request.observed_at,
        expires_at=request.expires_at,
        external_identity=(
            {
                "account_id": request.external_account_id,
                "sandbox_id": request.external_sandbox_id,
                "environment": request.external_environment,
                **(
                    {"verification": "DECLARED_CLIENT_IDENTITY_ONLY"}
                    if request.scope == "SIGNAL_QUALITY"
                    else {}
                ),
            }
            if request.is_external
            else None
        ),
        score_source=score_source,
        score_observed_at=score_observed_at,
    )
