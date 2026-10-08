from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.direct_evidence import TradeEvidence
from packages.connected.trade_assessment import (
    TradeRequest,
    evaluate,
    policy_metadata,
    proposal_fingerprint,
    validate_pass_receipt,
)
from packages.domain.options import Greeks, OptionContract, OptionQuote
from packages.domain.workflow import CatalystFeatures
from packages.strategy.catalyst import score_components

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
SYMBOL = "AAPL261106C00200000"


def proposal(**overrides):
    return TradeRequest.model_validate(
        {
            "legs": [{"symbol": SYMBOL, "side": "buy"}],
            "quantity": 1,
            "limit_price": "2",
            **overrides,
        }
    )


def evidence():
    return TradeEvidence(
        contracts=(
            OptionContract(
                contract_id="synthetic-test-fixture",
                symbol=SYMBOL,
                underlying_symbol="AAPL",
                expiration=date(2026, 11, 6),
                strike=Decimal(200),
                option_type="call",
                multiplier=100,
                tradable=True,
                quote=OptionQuote(
                    bid="1.95",
                    ask="2.05",
                    bid_size=10,
                    ask_size=10,
                    quoted_at=NOW,
                    open_interest=1000,
                    greeks=Greeks(delta=".5", gamma=".01", theta="-.01", vega=".1"),
                ),
            ),
        ),
        features=CatalystFeatures(
            catalyst_confidence=".9",
            sentiment=".8",
            relative_volume=3,
            price_momentum=".8",
            gap_percent=1,
            market_confirmation=".8",
            sector_confirmation=".8",
            liquidity_score=".9",
        ),
        features_at=NOW,
        spot=Decimal(200),
        greeks_at=NOW,
        greeks_source="synthetic-test-source",
    )


def alter_quote(ev, **changes):
    c = ev.contracts[0]
    return replace(
        ev, contracts=(c.model_copy(update={"quote": c.quote.model_copy(update=changes)}),)
    )


def assess(trade=None, ev=None, policy=None):
    return evaluate(trade or proposal(), policy or AssessmentPolicy(), ev or evidence(), now=NOW)


def test_pass_is_a_fresh_bound_assessment_never_order_authority():
    result = assess()
    assert result["decision"] == "PASS", result
    assert result["signal_score"] >= float(result["minimum_passing_score"])
    assert result["execution_allowed"] is False
    assert result["market_data"]["feed"] == "indicative"
    assert result["market_data"]["quality"] == "testing_only"
    assert result["market_data"]["greeks_calculation_provenance"] == "synthetic-test-source"
    assert result["market_data"]["source_quote_times"] == {SYMBOL: NOW.isoformat()}
    assert datetime.fromisoformat(result["expires_at"]) > NOW
    assert result["scope"] == "TRADE_ASSESSMENT"
    assert result["trade_fingerprint"] != assess(proposal(quantity=2))["trade_fingerprint"]
    assert result["score_profile"] == "catalyst_momentum_v1"
    assert result["score_components"] == {
        name: float(value) for name, value in score_components(evidence().features).items()
    }
    assert result["evidence_as_of"] == evidence().features_at.isoformat()


def test_proposal_fingerprint_matches_go_contract_and_normalizes_default_ratio_and_decimal():
    canonical = TradeRequest.model_validate(
        {"legs": [{"symbol": SYMBOL, "side": "buy"}], "quantity": 1, "limit_price": "2"}
    )
    explicit = TradeRequest.model_validate(
        {
            "legs": [{"symbol": SYMBOL, "side": "buy", "ratio_quantity": 1}],
            "quantity": 1,
            "limit_price": "2.00",
        }
    )
    expected = "b1f1afbeb11913b98726ec9d88584c0d271bef8826adc2a6034a24e9a88a02be"
    assert proposal_fingerprint(canonical) == expected
    assert proposal_fingerprint(explicit) == expected
    assert proposal_fingerprint(proposal(quantity=2)) != expected
    assert proposal_fingerprint(proposal(client_reference="client-1")) != expected
    metadata = policy_metadata(AssessmentPolicy())
    assert metadata["policy_version"].startswith("standalone-paper-advisory-v1:")
    assert metadata["advisory_policy_profile"] == "paper_advisory_greeks_v1"


@pytest.mark.parametrize(
    "time", [NOW - timedelta(days=1), NOW + timedelta(seconds=1), NOW.replace(tzinfo=None)]
)
def test_source_times_are_never_rewritten_as_fresh(time):
    result = assess(ev=replace(evidence(), features_at=time))
    assert result["decision"] == "UNAVAILABLE"
    assert result["signal_score"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "AAPL261106C00210000"),
        ("underlying_symbol", "MSFT"),
        ("strike", Decimal(201)),
        ("multiplier", 10),
    ],
)
def test_contract_identity_and_standard_multiplier_are_verified(field, value):
    ev = evidence()
    ev = replace(ev, contracts=(ev.contracts[0].model_copy(update={field: value}),))
    assert assess(ev=ev)["decision"] == "UNAVAILABLE"


def test_missing_greeks_are_unavailable_not_a_measured_failure():
    assert assess(ev=alter_quote(evidence(), greeks=None))["decision"] == "UNAVAILABLE"


def test_greeks_without_independent_source_time_are_advisory_with_warning():
    result = assess(ev=replace(evidence(), greeks_at=None, greeks_source=None))
    assert result["decision"] == "PASS"
    assert result["signal_score"] is not None
    assert result["market_data"]["greeks_calculation_provenance"] is None
    assert result["market_data"]["greeks_calculation_freshness"] == "unknown"
    assert result["market_data"]["warnings"] == ["greek_calculation_time_unknown"]
    assert result["market_data"]["source_quote_times"] == {SYMBOL: NOW.isoformat()}


def test_partial_greek_provenance_is_rejected_as_contradictory():
    result = assess(ev=replace(evidence(), greeks_at=None, greeks_source="provider"))
    assert result["decision"] == "UNAVAILABLE"
    assert result["blocking_reasons"] == ["greek_provenance_contradictory"]


@pytest.mark.parametrize(
    "greeks_at",
    [NOW - timedelta(days=1), NOW + timedelta(minutes=1), NOW.replace(tzinfo=None)],
)
def test_unavailable_greek_timestamps_are_not_reported_verified(greeks_at):
    result = assess(ev=replace(evidence(), greeks_at=greeks_at))
    assert result["decision"] == "UNAVAILABLE"
    assert result["market_data"]["greeks_calculated_at"] == greeks_at.isoformat()
    assert result["market_data"]["greeks_calculation_freshness"] == "invalid"


def test_unavailable_contradictory_greek_provenance_is_not_reported_verified():
    ev = replace(evidence(), greeks_at=None, greeks_source="provider")
    result = assess(ev=ev)
    assert result["decision"] == "UNAVAILABLE"
    assert result["market_data"]["greeks_calculation_freshness"] == "invalid"
    assert result["blocking_reasons"] == ["greek_provenance_contradictory"]


def test_pass_receipt_rejects_whitespace_greek_provenance():
    result = assess(ev=evidence())
    result["market_data"]["greeks_calculation_provenance"] = "   "
    assert not validate_pass_receipt(result, proposal(), now=NOW)


def test_advisory_receipt_is_policy_bound_and_never_authorizes_execution():
    result = assess(ev=replace(evidence(), greeks_at=None, greeks_source=None))
    assert result["paper_only"] and result["human_approval_required"]
    assert result["execution_allowed"] is False
    assert validate_pass_receipt(result, proposal(), now=NOW)
    forged = dict(result, advisory_policy_hash="0" * 64)
    assert not validate_pass_receipt(forged, proposal(), now=NOW)


def test_weak_catalyst_cannot_be_repaired_by_declaring_a_price_or_quantity():
    ev = evidence()
    ev = replace(ev, features=ev.features.model_copy(update={"catalyst_confidence": Decimal(".1")}))
    result = assess(ev=ev)
    assert result["decision"] == "FAIL"
    assert "weak_catalyst_confidence" in result["blocking_reasons"]
    assert result["signal_score"] is not None
    assert result["remediation"]


def test_bearish_signal_rejects_a_bullish_call():
    ev = evidence()
    ev = replace(
        ev,
        features=ev.features.model_copy(
            update={
                "sentiment": Decimal("-.8"),
                "price_momentum": Decimal("-.8"),
                "market_confirmation": Decimal("-.8"),
                "sector_confirmation": Decimal("-.8"),
            }
        ),
    )
    assert "signal_direction" in assess(ev=ev)["blocking_reasons"]


def test_neutral_rising_signal_matches_bullish_score_tie_break_in_assessment():
    ev = evidence()
    ev = replace(
        ev,
        features=ev.features.model_copy(update={"sentiment": Decimal("0")}),
    )

    result = assess(ev=ev)

    assert result["decision"] == "PASS", result
    assert result["signal_score"] >= float(result["minimum_passing_score"])
    assert "price_action_not_confirming" not in result["blocking_reasons"]
    checks = {check["code"]: check for check in result["checks"]}
    assert checks["signal_direction"]["passed"] is True
    assert "price_action_not_confirming" not in checks
    assert checks["evidence_fresh"]["passed"] is True
    assert checks["policy_loss"]["passed"] is True


def test_risk_budget_is_measured_on_proposed_debit_and_total_quantity():
    result = assess(proposal(quantity=10))
    assert result["decision"] == "FAIL"
    assert result["signal_score"] is not None
    assert any(not c["passed"] and "loss" in c["code"] for c in result["checks"])


def test_no_quote_depth_fails_without_inventing_a_size():
    result = assess(ev=alter_quote(evidence(), bid_size=0, ask_size=0))
    assert result["decision"] == "FAIL"


def test_naked_short_is_explicitly_unsupported():
    result = assess(proposal(legs=[{"symbol": SYMBOL, "side": "sell"}]))
    assert result["decision"] == "UNAVAILABLE"
    assert result["retryable"] is False
    assert result["blocking_reasons"] == ["unsupported_strategy"]


@pytest.mark.parametrize(
    "extra",
    [
        "signal_score",
        "market_scanner_features",
        "market_evidence_at",
        "max_loss",
        "external_account_id",
        "standalone",
    ],
)
def test_caller_cannot_declare_scoring_evidence_or_execution_authority(extra):
    with pytest.raises(ValueError):
        proposal(**{extra: 123})
