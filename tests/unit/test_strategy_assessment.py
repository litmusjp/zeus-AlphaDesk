from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.strategy_assessment import (
    StrategyAssessmentRequest,
    assess_strategy,
)


def payload(**overrides: object) -> StrategyAssessmentRequest:
    now = datetime.now(UTC)
    value: dict[str, object] = {
        "underlying_symbol": "AAPL",
        "strategy_type": "bull_call_debit_spread",
        "side": "buy",
        "quantity": 1,
        "limit_price": "1.20",
        "observed_at": now,
        "expires_at": now + timedelta(seconds=30),
        "legs": [
            {
                "symbol": "AAPL260117C00200000",
                "side": "buy",
                "quantity": 1,
                "price": "2.00",
                "bid": "1.90",
                "ask": "2.00",
                "open_interest": 100,
                "quote_size": "10",
                "quoted_at": now,
                "delta": "0.5",
                "gamma": "0.01",
                "theta": "-0.02",
                "vega": "0.1",
            },
            {
                "symbol": "AAPL260117C00210000",
                "side": "sell",
                "quantity": 1,
                "price": "0.80",
                "bid": "0.80",
                "ask": "0.90",
                "open_interest": 100,
                "quote_size": "10",
                "quoted_at": now,
                "delta": "0.3",
                "gamma": "0.01",
                "theta": "-0.02",
                "vega": "0.1",
            },
        ],
        "max_loss": "40",
        "greeks": {"delta": "0.2", "gamma": "0", "theta": "0", "vega": "0"},
        "market_evidence_at": now,
    }
    value.update(overrides)
    return StrategyAssessmentRequest.model_validate(value)


def test_assessment_reuses_policy_and_returns_stable_checks() -> None:
    result = assess_strategy(payload(), AssessmentPolicy(), paper_equity=Decimal("10000"))
    assert result.pass_ is True
    assert result.decision == "PASS"
    assert {check.code for check in result.checks} >= {
        "strategy_shape",
        "policy_loss",
        "evidence_fresh",
    }
    assert result.paper_only is True
    assert result.human_approval_required is True
    assert result.execution_allowed is False


def test_malformed_or_stale_strategy_fails_closed() -> None:
    now = datetime.now(UTC)
    request = payload(
        market_evidence_at=now - timedelta(hours=1), expires_at=now + timedelta(seconds=30)
    )
    result = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    assert result.pass_ is False
    assert result.decision in {"FAIL", "UNAVAILABLE"}
    assert (
        "evidence_fresh" in result.failed_check_codes
        or "strategy_shape" in result.failed_check_codes
    )
