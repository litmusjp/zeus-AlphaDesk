from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.broker import BrokerPosition
from packages.risk.engine import RiskPolicy


class AssessmentProfile(StrEnum):
    VERY_CONSERVATIVE = "VERY_CONSERVATIVE"
    CONSERVATIVE = "CONSERVATIVE"
    FAIR = "FAIR"
    AGGRESSIVE = "AGGRESSIVE"
    VERY_AGGRESSIVE = "VERY_AGGRESSIVE"


ASSESSMENT_PROFILE_LABELS: dict[AssessmentProfile, str] = {
    AssessmentProfile.VERY_CONSERVATIVE: "Very Conservative",
    AssessmentProfile.CONSERVATIVE: "Conservative",
    AssessmentProfile.FAIR: "Fair",
    AssessmentProfile.AGGRESSIVE: "Aggressive",
    AssessmentProfile.VERY_AGGRESSIVE: "Very Aggressive",
}


# This is the only source of truth for named policy profiles. Values are kept
# as JSON-compatible scalars so the same materializer serves API and workers.
ASSESSMENT_PROFILE_VALUES: dict[AssessmentProfile, dict[str, object]] = {
    AssessmentProfile.VERY_CONSERVATIVE: {
        "minimum_signal_score": "80",
        "pre_scan_minimum_signal_score": "60",
        "maximum_gap_percent": "5",
        "minimum_catalyst_confidence": "0.80",
        "pre_scan_minimum_catalyst_confidence": "0.60",
        "minimum_dte": 30,
        "maximum_dte": 60,
        "execution_max_spread_ratio": "0.10",
        "pre_scan_max_spread_ratio": "0.30",
        "execution_min_open_interest": 1000,
        "pre_scan_min_open_interest": 250,
        "execution_max_quote_age_seconds": 30,
        "pre_scan_max_quote_age_seconds": 3600,
        "minimum_quote_size": "5",
        "maximum_strike_distance_ratio": "0.10",
        "require_greeks": True,
        "max_investment_per_candidate": "100",
        "maximum_contracts_per_candidate": 1,
        "max_planned_loss_per_trade_pct_equity": "0.25",
        "max_total_open_planned_loss_pct_equity": "1.5",
        "max_underlying_risk_concentration_pct": "5",
        "daily_loss_halt_pct_equity": "1",
        "portfolio_drawdown_halt_pct": "5",
        "max_concurrent_option_structures": 3,
        "max_abs_portfolio_delta": "1000",
        "max_abs_portfolio_gamma": "100",
        "max_abs_portfolio_theta": "100",
        "max_abs_portfolio_vega": "1000",
    },
    AssessmentProfile.CONSERVATIVE: {
        "minimum_signal_score": "65",
        "pre_scan_minimum_signal_score": "50",
        "maximum_gap_percent": "12",
        "minimum_catalyst_confidence": "0.60",
        "pre_scan_minimum_catalyst_confidence": "0.40",
        "minimum_dte": 14,
        "maximum_dte": 45,
        "execution_max_spread_ratio": "0.20",
        "pre_scan_max_spread_ratio": "1.00",
        "execution_min_open_interest": 25,
        "pre_scan_min_open_interest": None,
        "execution_max_quote_age_seconds": 120,
        "pre_scan_max_quote_age_seconds": 86400,
        "minimum_quote_size": "1",
        "maximum_strike_distance_ratio": "0.30",
        "require_greeks": True,
        "max_investment_per_candidate": "250",
        "maximum_contracts_per_candidate": 10,
        "max_planned_loss_per_trade_pct_equity": "0.50",
        "max_total_open_planned_loss_pct_equity": "4.0",
        "max_underlying_risk_concentration_pct": "10",
        "daily_loss_halt_pct_equity": "2",
        "portfolio_drawdown_halt_pct": "10",
        "max_concurrent_option_structures": 8,
        "max_abs_portfolio_delta": "5000",
        "max_abs_portfolio_gamma": "1000",
        "max_abs_portfolio_theta": "1000",
        "max_abs_portfolio_vega": "5000",
    },
    AssessmentProfile.FAIR: {
        "minimum_signal_score": "55",
        "pre_scan_minimum_signal_score": "40",
        "maximum_gap_percent": "20",
        "minimum_catalyst_confidence": "0.45",
        "pre_scan_minimum_catalyst_confidence": "0.30",
        "minimum_dte": 7,
        "maximum_dte": 60,
        "execution_max_spread_ratio": "0.30",
        "pre_scan_max_spread_ratio": "1.25",
        "execution_min_open_interest": 10,
        "pre_scan_min_open_interest": 10,
        "execution_max_quote_age_seconds": 300,
        "pre_scan_max_quote_age_seconds": 172800,
        "minimum_quote_size": "1",
        "maximum_strike_distance_ratio": "0.30",
        "require_greeks": True,
        "max_investment_per_candidate": "500",
        "maximum_contracts_per_candidate": 10,
        "max_planned_loss_per_trade_pct_equity": "1.00",
        "max_total_open_planned_loss_pct_equity": "6.0",
        "max_underlying_risk_concentration_pct": "15",
        "daily_loss_halt_pct_equity": "3",
        "portfolio_drawdown_halt_pct": "12.5",
        "max_concurrent_option_structures": 15,
        "max_abs_portfolio_delta": "10000",
        "max_abs_portfolio_gamma": "2500",
        "max_abs_portfolio_theta": "2500",
        "max_abs_portfolio_vega": "10000",
    },
    AssessmentProfile.AGGRESSIVE: {
        "minimum_signal_score": "45",
        "pre_scan_minimum_signal_score": "30",
        "maximum_gap_percent": "30",
        "minimum_catalyst_confidence": "0.30",
        "pre_scan_minimum_catalyst_confidence": "0.20",
        "minimum_dte": 3,
        "maximum_dte": 90,
        "execution_max_spread_ratio": "0.40",
        "pre_scan_max_spread_ratio": "1.50",
        "execution_min_open_interest": 5,
        "pre_scan_min_open_interest": None,
        "execution_max_quote_age_seconds": 300,
        "pre_scan_max_quote_age_seconds": 604800,
        "minimum_quote_size": "1",
        "maximum_strike_distance_ratio": "0.30",
        "require_greeks": True,
        "max_investment_per_candidate": "750",
        "maximum_contracts_per_candidate": 10,
        "max_planned_loss_per_trade_pct_equity": "2.00",
        "max_total_open_planned_loss_pct_equity": "8.0",
        "max_underlying_risk_concentration_pct": "20",
        "daily_loss_halt_pct_equity": "4",
        "portfolio_drawdown_halt_pct": "15",
        "max_concurrent_option_structures": 30,
        "max_abs_portfolio_delta": "25000",
        "max_abs_portfolio_gamma": "10000",
        "max_abs_portfolio_theta": "10000",
        "max_abs_portfolio_vega": "25000",
    },
    AssessmentProfile.VERY_AGGRESSIVE: {
        "minimum_signal_score": "35",
        "pre_scan_minimum_signal_score": "20",
        "maximum_gap_percent": "50",
        "minimum_catalyst_confidence": "0.20",
        "pre_scan_minimum_catalyst_confidence": "0.10",
        "minimum_dte": 1,
        "maximum_dte": 180,
        "execution_max_spread_ratio": "0.50",
        "pre_scan_max_spread_ratio": "2.00",
        "execution_min_open_interest": 1,
        "pre_scan_min_open_interest": None,
        "execution_max_quote_age_seconds": 300,
        "pre_scan_max_quote_age_seconds": 604800,
        "minimum_quote_size": "1",
        "maximum_strike_distance_ratio": "0.30",
        "require_greeks": True,
        "max_investment_per_candidate": "1000",
        "maximum_contracts_per_candidate": 10,
        "max_planned_loss_per_trade_pct_equity": "3.00",
        "max_total_open_planned_loss_pct_equity": "10.0",
        "max_underlying_risk_concentration_pct": "25",
        "daily_loss_halt_pct_equity": "5",
        "portfolio_drawdown_halt_pct": "20",
        "max_concurrent_option_structures": 100,
        "max_abs_portfolio_delta": "100000",
        "max_abs_portfolio_gamma": "100000",
        "max_abs_portfolio_theta": "100000",
        "max_abs_portfolio_vega": "100000",
    },
}


class AssessmentPolicy(BaseModel):
    """Workspace-scoped candidate selection and sizing controls.

    These controls tune discovery only. Broker, Guardian, freshness, approval,
    account binding, and paper-only enforcement remain hard gates elsewhere.
    """

    model_config = ConfigDict(frozen=True)

    minimum_signal_score: Decimal = Field(default=Decimal("65"), ge=Decimal("0"), le=Decimal("100"))
    # Discovery-only underlying thresholds. Units are score points (0-100) and
    # catalyst confidence (0-1); execution always uses the strict fields above.
    pre_scan_minimum_signal_score: Decimal = Field(
        default=Decimal("50"), ge=Decimal("0"), le=Decimal("100")
    )
    maximum_gap_percent: Decimal = Field(default=Decimal("12"), ge=Decimal("0"), le=Decimal("100"))
    minimum_catalyst_confidence: Decimal = Field(
        default=Decimal("0.60"), ge=Decimal("0"), le=Decimal("1")
    )
    pre_scan_minimum_catalyst_confidence: Decimal = Field(
        default=Decimal("0.40"), ge=Decimal("0"), le=Decimal("1")
    )
    minimum_dte: int = Field(default=14, ge=1, le=365)
    maximum_dte: int = Field(default=45, ge=1, le=365)
    pre_scan_max_spread_ratio: Decimal = Field(
        default=Decimal("1.00"), ge=Decimal("0"), le=Decimal("2")
    )
    pre_scan_min_open_interest: int | None = Field(default=None, ge=0, le=1_000_000)
    pre_scan_max_quote_age_seconds: int = Field(default=86400, ge=0, le=604800)
    execution_max_spread_ratio: Decimal = Field(
        default=Decimal("0.20"), ge=Decimal("0"), le=Decimal("0.50")
    )
    execution_min_open_interest: int = Field(default=25, ge=1, le=1_000_000)
    execution_max_quote_age_seconds: int = Field(default=120, ge=0, le=300)
    minimum_quote_size: Decimal = Field(default=Decimal("1"), gt=0, le=100000)
    maximum_strike_distance_ratio: Decimal = Field(
        default=Decimal("0.30"), ge=0, le=Decimal("0.30")
    )
    require_greeks: Literal[True] = True
    max_investment_per_candidate: Decimal = Field(default=Decimal("250"), gt=0, le=Decimal("1000"))
    maximum_contracts_per_candidate: int = Field(default=10, ge=1, le=10)
    max_planned_loss_per_trade_pct_equity: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=Decimal("5")
    )
    max_total_open_planned_loss_pct_equity: Decimal = Field(
        default=Decimal("4.0"), gt=0, le=Decimal("10")
    )
    max_underlying_risk_concentration_pct: Decimal = Field(
        default=Decimal("10.0"), gt=0, le=Decimal("25")
    )
    daily_loss_halt_pct_equity: Decimal = Field(default=Decimal("2.0"), gt=0, le=Decimal("5"))
    portfolio_drawdown_halt_pct: Decimal = Field(default=Decimal("10.0"), gt=0, le=Decimal("20"))
    max_concurrent_option_structures: int = Field(default=8, ge=1, le=100)
    max_abs_portfolio_delta: Decimal = Field(default=Decimal("5000"), gt=0, le=Decimal("1000000"))
    max_abs_portfolio_gamma: Decimal = Field(default=Decimal("1000"), gt=0, le=Decimal("1000000"))
    max_abs_portfolio_theta: Decimal = Field(default=Decimal("1000"), gt=0, le=Decimal("1000000"))
    max_abs_portfolio_vega: Decimal = Field(default=Decimal("5000"), gt=0, le=Decimal("1000000"))

    @model_validator(mode="after")
    def validate_ranges(self) -> AssessmentPolicy:
        if self.pre_scan_minimum_signal_score > self.minimum_signal_score:
            raise ValueError("pre-scan signal score cannot be stricter than execution")
        if self.pre_scan_minimum_catalyst_confidence > self.minimum_catalyst_confidence:
            raise ValueError("pre-scan catalyst confidence cannot be stricter than execution")
        if self.maximum_dte < self.minimum_dte:
            raise ValueError("maximum_dte must be greater than or equal to minimum_dte")
        if self.execution_max_spread_ratio > self.pre_scan_max_spread_ratio:
            raise ValueError("execution spread ratio cannot be looser than pre-scan")
        if self.execution_max_quote_age_seconds > self.pre_scan_max_quote_age_seconds:
            raise ValueError("execution quote age cannot be looser than pre-scan")
        if (
            self.pre_scan_min_open_interest is not None
            and self.execution_min_open_interest < self.pre_scan_min_open_interest
        ):
            raise ValueError("execution open interest cannot be looser than pre-scan")
        return self

    @classmethod
    def from_payload(cls, payload: dict[str, object] | None) -> AssessmentPolicy:
        return cls.model_validate(payload or {})

    def order_sizing_error(self, *, quantity: int, max_loss: Decimal) -> str | None:
        if quantity > self.maximum_contracts_per_candidate:
            return "contract quantity exceeds policy cap"
        if max_loss > self.max_investment_per_candidate:
            return "investment size exceeds policy cap"
        return None

    def as_risk_policy(self) -> RiskPolicy:
        return RiskPolicy(
            max_planned_loss_per_trade_pct_equity=self.max_planned_loss_per_trade_pct_equity,
            max_total_open_planned_loss_pct_equity=self.max_total_open_planned_loss_pct_equity,
            max_underlying_risk_concentration_pct=self.max_underlying_risk_concentration_pct,
            daily_loss_halt_pct_equity=self.daily_loss_halt_pct_equity,
            portfolio_drawdown_halt_pct=self.portfolio_drawdown_halt_pct,
            max_concurrent_option_structures=self.max_concurrent_option_structures,
            max_abs_portfolio_delta=self.max_abs_portfolio_delta,
            max_abs_portfolio_gamma=self.max_abs_portfolio_gamma,
            max_abs_portfolio_theta=self.max_abs_portfolio_theta,
            max_abs_portfolio_vega=self.max_abs_portfolio_vega,
        )


def materialize_profile(profile: AssessmentProfile) -> AssessmentPolicy:
    """Build a validated policy from the server-owned profile table."""
    return AssessmentPolicy.model_validate(ASSESSMENT_PROFILE_VALUES[profile])


def profile_for_policy(policy: AssessmentPolicy) -> AssessmentProfile | None:
    """Return the exact named profile, or None for a manually customized policy."""
    for profile in AssessmentProfile:
        if policy == materialize_profile(profile):
            return profile
    return None


def position_exposure(
    positions: tuple[BrokerPosition, ...], underlying_symbol: str, paper_equity: Decimal
) -> tuple[Decimal, Decimal, bool]:
    option_positions = tuple(
        position for position in positions if position.asset_class.lower() == "us_option"
    )
    # Broker projections do not contain option strategy max-loss or Greeks. Treat
    # any existing option position as fully exposed until a strategy-aware
    # projection exists; underestimating it would weaken aggregate risk checks.
    option_exposure = paper_equity if option_positions else Decimal("0")
    matching_underlying_exposure = sum(
        (
            abs(position.cost_basis)
            for position in positions
            if position.symbol.upper() == underlying_symbol.upper()
        ),
        Decimal("0"),
    )
    return option_exposure, option_exposure + matching_underlying_exposure, not positions
