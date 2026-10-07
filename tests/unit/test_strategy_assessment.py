from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.strategy_assessment import (
    StrategyAssessmentRequest,
    assess_strategy,
)
from packages.domain.workflow import CatalystFeatures
from packages.strategy.catalyst import score_signal


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


def test_legacy_assessment_still_requires_open_interest_when_threshold_is_unset():
    request = payload()
    missing_oi_leg = request.legs[0].model_copy(update={"open_interest": None})
    request = request.model_copy(update={"legs": (missing_oi_leg,)})
    policy = AssessmentPolicy.model_construct(execution_min_open_interest=None)
    result = assess_strategy(request, policy, paper_equity=Decimal("10000"))
    liquidity = next(check for check in result.checks if check.code == "leg_0_liquidity")
    assert liquidity.passed is False


def test_signal_quality_scope_does_not_require_broker_equity() -> None:
    now = datetime.now(UTC)
    result = assess_strategy(
        payload(
            scope="SIGNAL_QUALITY",
            external_account_id="declared-paper",
            external_sandbox_id="declared-sandbox",
            external_environment="PAPER",
        ),
        AssessmentPolicy(),
        broker_evidence_available=False,
        now=now,
        trusted_market_features=CatalystFeatures(
            catalyst_confidence="1", sentiment="1", relative_volume="4",
            price_momentum="1", gap_percent="0", market_confirmation="1",
            sector_confirmation="1", liquidity_score="1",
        ),
        trusted_score_source="alphadesk_connected_opportunity",
        trusted_score_observed_at=now,
        trusted_signal_direction="BULLISH",
    )
    assert result.scope == "SIGNAL_QUALITY"
    assert "external_account_evidence" not in result.failed_check_codes
    assert "external_equity_evidence" not in result.failed_check_codes


def test_assessment_returns_canonical_market_scanner_signal_score() -> None:
    features = CatalystFeatures(
        catalyst_confidence="0.90",
        sentiment="0.80",
        relative_volume="3.0",
        price_momentum="0.70",
        gap_percent="1.0",
        market_confirmation="0.60",
        sector_confirmation="0.50",
        liquidity_score="0.90",
    )
    result = assess_strategy(
        payload(market_scanner_features=features),
        AssessmentPolicy(),
        paper_equity=Decimal("10000"),
    )
    assert result.market_scanner_signal_score == score_signal(features)


def test_assessment_returns_no_signal_score_without_market_features() -> None:
    result = assess_strategy(payload(), AssessmentPolicy(), paper_equity=Decimal("10000"))
    assert result.market_scanner_signal_score is None


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


def test_native_assessment_keeps_broker_evidence_and_optional_external_identity() -> None:
    result = assess_strategy(
        payload(), AssessmentPolicy(), paper_equity=Decimal("10000"), broker_evidence_available=True
    )
    assert result.decision == "PASS"
    assert "broker_evidence" in {check.code for check in result.checks}
    assert result.external_identity is None


def test_external_assessment_requires_identity_and_missing_external_evidence_is_unavailable() -> (
    None
):
    result = assess_strategy(
        payload(
            external_account_id="op-paper-l1",
            external_sandbox_id="op-sandbox-l1",
            external_environment="PAPER",
        ),
        AssessmentPolicy(),
        paper_equity=None,
        broker_evidence_available=False,
    )
    assert result.decision == "UNAVAILABLE"
    assert result.pass_ is False
    assert "external_account_evidence" in result.failed_check_codes
    assert "external_equity_evidence" in result.failed_check_codes
    assert result.external_identity == {
        "account_id": "op-paper-l1",
        "sandbox_id": "op-sandbox-l1",
        "environment": "PAPER",
    }


def test_native_four_leg_shape_preserves_existing_contract() -> None:
    base = payload().model_dump(mode="python")
    base["legs"] = tuple(
        [*base["legs"], base["legs"][0], base["legs"][1]]
    )
    result = assess_strategy(
        StrategyAssessmentRequest.model_validate(base),
        AssessmentPolicy(),
        paper_equity=Decimal("10000"),
    )
    assert result.decision == "PASS"
    assert "strategy_shape" not in result.failed_check_codes


def test_signal_quality_uses_occ_type_not_root_char_and_requires_typed_strategy() -> None:
    request = payload(
        scope="SIGNAL_QUALITY",
        external_account_id="declared-paper",
        external_sandbox_id="declared-sandbox",
        external_environment="PAPER",
        strategy_type="bull_call_debit_spread",
        legs=(
            {
                **payload().legs[0].model_dump(),
                "symbol": "C260117P00200000",
            },
        ),
    )
    result = assess_strategy(
        request,
        AssessmentPolicy(),
        broker_evidence_available=False,
        trusted_market_features=CatalystFeatures(
            catalyst_confidence="1", sentiment="-1", relative_volume="4",
            price_momentum="-1", gap_percent="0", market_confirmation="-1",
            sector_confirmation="-1", liquidity_score="1",
        ),
        trusted_score_source="alphadesk_connected_opportunity",
        trusted_score_observed_at=request.market_evidence_at,
        trusted_signal_direction="BEARISH",
        now=request.market_evidence_at,
    )
    assert result.decision == "FAIL"
    assert "strategy_shape" in result.failed_check_codes


def test_signal_quality_rejects_malformed_option_identity() -> None:
    request = payload(
        scope="SIGNAL_QUALITY",
        external_account_id="declared-paper",
        external_sandbox_id="declared-sandbox",
        external_environment="PAPER",
        legs=tuple(
            leg.model_copy(update={"symbol": "AAPL-not-an-occ-identity"})
            if index == 0
            else leg
            for index, leg in enumerate(payload().legs)
        ),
    )
    result = assess_strategy(
        request,
        AssessmentPolicy(),
        broker_evidence_available=False,
        trusted_market_features=CatalystFeatures(
            catalyst_confidence="1", sentiment="1", relative_volume="4",
            price_momentum="1", gap_percent="0", market_confirmation="1",
            sector_confirmation="1", liquidity_score="1",
        ),
        trusted_score_source="alphadesk_connected_opportunity",
        trusted_score_observed_at=request.market_evidence_at,
        trusted_signal_direction="BULLISH",
        now=request.market_evidence_at,
    )
    assert result.decision == "FAIL"
    assert "strategy_shape" in result.failed_check_codes


def test_future_market_and_leg_quotes_are_unavailable() -> None:
    now = datetime.now(UTC)
    request = payload(
        scope="SIGNAL_QUALITY",
        external_account_id="declared-paper",
        external_sandbox_id="declared-sandbox",
        external_environment="PAPER",
        market_evidence_at=now + timedelta(seconds=1),
        legs=tuple(
            leg.model_copy(update={"quoted_at": now + timedelta(seconds=1)})
            for leg in payload().legs
        ),
    )
    result = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"), now=now)
    assert result.decision == "UNAVAILABLE"
    assert "evidence_fresh" in result.failed_check_codes
    assert "leg_0_quote_fresh" in result.failed_check_codes


def test_stale_leg_quote_without_stale_market_evidence_is_unavailable() -> None:
    now = datetime.now(UTC)
    request = payload(
        scope="SIGNAL_QUALITY",
        external_account_id="declared-paper",
        external_sandbox_id="declared-sandbox",
        external_environment="PAPER",
        legs=tuple(
            leg.model_copy(update={"quoted_at": now - timedelta(hours=1)})
            for leg in payload().legs
        ),
    )
    result = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"), now=now)
    assert result.decision == "UNAVAILABLE"
    assert "leg_0_quote_fresh" in result.failed_check_codes

    native_result = assess_strategy(
        payload(
            market_evidence_at=now,
            legs=tuple(
                leg.model_copy(update={"quoted_at": now - timedelta(hours=1)})
                for leg in payload().legs
            ),
        ),
        AssessmentPolicy(),
        paper_equity=Decimal("10000"),
        now=now,
    )
    assert native_result.decision == "PASS"
    assert "leg_0_quote_fresh" not in native_result.failed_check_codes
