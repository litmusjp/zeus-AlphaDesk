from decimal import Decimal

import pytest

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.option_scan_policy import ScanMode, select_contracts
from packages.domain.options import OptionType
from tests.unit.test_option_scan_policy import AS_OF, contract


def test_policy_defaults_are_safe_and_translate_to_risk_policy() -> None:
    policy = AssessmentPolicy()
    risk = policy.as_risk_policy()

    assert policy.minimum_signal_score == Decimal("65")
    assert policy.pre_scan_minimum_signal_score == Decimal("50")
    assert policy.minimum_catalyst_confidence == Decimal("0.60")
    assert policy.pre_scan_minimum_catalyst_confidence == Decimal("0.40")
    assert policy.max_investment_per_candidate == Decimal("250")
    assert risk.max_planned_loss_per_trade_pct_equity == Decimal("0.50")
    assert risk.max_concurrent_option_structures == 8


def test_policy_rejects_inverted_expiry_range() -> None:
    with pytest.raises(ValueError, match="maximum_dte"):
        AssessmentPolicy(minimum_dte=60, maximum_dte=30)


def test_order_sizing_rejects_cap_breaches() -> None:
    policy = AssessmentPolicy(
        max_investment_per_candidate=Decimal("250"), maximum_contracts_per_candidate=2
    )
    assert policy.order_sizing_error(quantity=2, max_loss=Decimal("100")) is None
    assert (
        policy.order_sizing_error(quantity=3, max_loss=Decimal("100"))
        == "contract quantity exceeds policy cap"
    )
    assert (
        policy.order_sizing_error(quantity=2, max_loss=Decimal("300"))
        == "investment size exceeds policy cap"
    )


def test_execution_filters_cannot_be_looser_than_pre_scan() -> None:
    with pytest.raises(ValueError, match="spread ratio"):
        AssessmentPolicy(
            pre_scan_max_spread_ratio=Decimal("0.10"),
            execution_max_spread_ratio=Decimal("0.20"),
        )


def test_pre_scan_underlying_thresholds_cannot_be_stricter_than_execution() -> None:
    with pytest.raises(ValueError, match="signal score"):
        AssessmentPolicy(pre_scan_minimum_signal_score=Decimal("66"))
    with pytest.raises(ValueError, match="catalyst confidence"):
        AssessmentPolicy(pre_scan_minimum_catalyst_confidence=Decimal("0.61"))


def test_older_policy_payload_gets_new_pre_scan_underlying_defaults() -> None:
    policy = AssessmentPolicy.from_payload(
        {"minimum_signal_score": "65", "minimum_catalyst_confidence": "0.60"}
    )

    assert policy.pre_scan_minimum_signal_score == Decimal("50")
    assert policy.pre_scan_minimum_catalyst_confidence == Decimal("0.40")
    assert policy.model_dump(mode="json")["pre_scan_minimum_signal_score"] == "50"


def test_custom_pre_scan_liquidity_controls_are_applied() -> None:
    selected = select_contracts(
        (contract(quoted_at=AS_OF),),
        underlying_price=Decimal("200"),
        wanted_type=OptionType.CALL,
        as_of=AS_OF,
        mode=ScanMode.PRE_SCAN,
        policy=AssessmentPolicy(
            pre_scan_max_spread_ratio=Decimal("0.05"),
            execution_max_spread_ratio=Decimal("0.05"),
        ),
    )

    assert selected.selected == ()
    assert selected.diagnostics.strict_eligible_contracts == 0
    assert selected.diagnostics.rejection_counts["spread_too_wide"] == 1
