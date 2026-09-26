from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
    request_autonomous_paper_authorization: bool = False

    @model_validator(mode="after")
    def validate_timestamps(self) -> StrategyAssessmentRequest:
        timestamps = (self.market_evidence_at, self.observed_at, self.expires_at)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("assessment timestamps must be timezone-aware")
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        return self


class AssessmentCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    passed: bool
    actual: Any = None
    limit: Any = None
    reason: str


class AutonomousPaperAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    authorization_id: UUID | None = None
    fingerprint: str | None = None
    mode: Literal["PAPER_ONLY"] | None = None
    environment: Literal["PAPER"] | None = None
    workspace_id: str | None = None
    account_id: str | None = None
    issuer: str = "AlphaDesk"
    source: str = "strategy_assessment"
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    policy_version: str | None = None
    strategy_identity: dict[str, Any] | None = None


class StrategyAssessmentResult(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    pass_: bool = Field(alias="pass")
    decision: Literal["PASS", "FAIL", "UNAVAILABLE"]
    assessment_id: UUID = Field(default_factory=uuid4)
    strategy_identity: dict[str, Any]
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
    autonomous_paper_authorization: AutonomousPaperAuthorization | None = None


def _check(code: str, passed: bool, actual: Any, limit: Any, reason: str) -> AssessmentCheck:
    return AssessmentCheck(code=code, passed=passed, actual=actual, limit=limit, reason=reason)


def _strategy_authorization_identity(request: StrategyAssessmentRequest) -> dict[str, Any]:
    return {
        "underlying_symbol": request.underlying_symbol.upper(),
        "strategy_type": request.strategy_type,
        "side": request.side,
        "quantity": request.quantity,
        "limit_price": format(request.limit_price, "f"),
        "max_loss": format(request.max_loss, "f"),
        "legs": [
            {
                "symbol": leg.symbol,
                "side": leg.side,
                "quantity": leg.quantity,
                "price": format(leg.price, "f"),
            }
            for leg in request.legs
        ],
    }


def build_autonomous_paper_authorization(
    request: StrategyAssessmentRequest,
    assessment: StrategyAssessmentResult,
    *,
    request_autonomous_paper_authorization: bool,
    policy_enabled: bool,
    workspace_id: str | UUID | None,
    account_id: str | None,
    environment: str = "PAPER",
    guardian_ready: bool = False,
    broker_evidence_available: bool = True,
    now: datetime | None = None,
) -> AutonomousPaperAuthorization | None:
    if not request_autonomous_paper_authorization:
        return None
    current = now or datetime.now(UTC)
    identity = _strategy_authorization_identity(request)
    denial: str | None = None
    if not policy_enabled:
        denial = "autonomous_paper_authorization_disabled"
    elif not assessment.pass_ or assessment.decision != "PASS":
        denial = "assessment_not_pass"
    elif not broker_evidence_available:
        denial = "broker_evidence_unavailable"
    elif not workspace_id:
        denial = "workspace_identity_missing"
    elif not account_id:
        denial = "paper_account_identity_missing"
    elif environment.upper() != "PAPER":
        denial = "paper_environment_required"
    elif not guardian_ready:
        denial = "guardian_prerequisites_not_satisfied"
    elif current >= request.expires_at:
        denial = "assessment_expired"

    if denial is not None:
        return AutonomousPaperAuthorization(allowed=False, reason=denial)

    binding = {
        "strategy_identity": identity,
        "workspace_id": str(workspace_id),
        "account_id": account_id,
        "environment": "PAPER",
        "policy_version": assessment.policy_version,
        "expires_at": request.expires_at.isoformat(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AutonomousPaperAuthorization(
        allowed=True,
        reason="authorized_for_paper_assessment_only",
        authorization_id=uuid5(NAMESPACE_URL, f"alphadesk:paper:{fingerprint}"),
        fingerprint=fingerprint,
        mode="PAPER_ONLY",
        environment="PAPER",
        workspace_id=str(workspace_id),
        account_id=account_id,
        issued_at=current,
        expires_at=request.expires_at,
        policy_version=assessment.policy_version,
        strategy_identity=identity,
    )


def assess_strategy(
    request: StrategyAssessmentRequest,
    policy: AssessmentPolicy,
    *,
    paper_equity: Decimal | None = None,
    broker_evidence_available: bool = True,
    now: datetime | None = None,
    policy_updated_at: datetime | None = None,
) -> StrategyAssessmentResult:
    current = now or datetime.now(UTC)
    age = max(Decimal("0"), Decimal(str((current - request.market_evidence_at).total_seconds())))
    checks: list[AssessmentCheck] = []
    checks.append(
        _check(
            "strategy_shape",
            len(request.legs) in (1, 2, 3, 4),
            len(request.legs),
            "1 to 4",
            "Supported option strategy shape.",
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
    checks.append(
        _check(
            "broker_evidence",
            broker_evidence_available,
            broker_evidence_available,
            True,
            "Fresh reconciled paper account evidence is required.",
        )
    )
    checks.append(
        _check(
            "evidence_fresh",
            age <= policy.execution_max_quote_age_seconds and current < request.expires_at,
            age,
            policy.execution_max_quote_age_seconds,
            "Market evidence must be current and unexpired.",
        )
    )
    for index, leg in enumerate(request.legs):
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
                "Each leg must have a usable execution quote.",
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
    if paper_equity is None or paper_equity <= 0:
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
    failed = tuple(check.code for check in checks if not check.passed)
    decision: Literal["PASS", "FAIL", "UNAVAILABLE"] = (
        "PASS"
        if not failed
        else (
            "UNAVAILABLE"
            if any(code in failed for code in ("broker_evidence", "paper_equity", "evidence_fresh"))
            else "FAIL"
        )
    )
    market_scanner_signal_score = (
        score_signal(request.market_scanner_features)
        if request.market_scanner_features is not None
        else None
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
        market_scanner_signal_score=market_scanner_signal_score,
        checks=tuple(checks),
        failed_check_codes=failed,
        policy_snapshot=policy.model_dump(mode="json"),
        policy_version="candidate-assessment-v1",
        policy_updated_at=policy_updated_at,
        market_evidence_at=request.market_evidence_at,
        observed_at=request.observed_at,
        expires_at=request.expires_at,
    )
