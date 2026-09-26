from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.strategy_assessment import (
    StrategyAssessmentRequest,
    assess_strategy,
    build_autonomous_paper_authorization,
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


def test_one_leg_long_call_can_pass_and_receive_paper_authorization() -> None:
    request = payload(
        strategy_type="long_call",
        legs=(payload().legs[0],),
        request_autonomous_paper_authorization=True,
    )
    assessment = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    authorization = build_autonomous_paper_authorization(
        request,
        assessment,
        request_autonomous_paper_authorization=request.request_autonomous_paper_authorization,
        policy_enabled=True,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
    )

    assert assessment.pass_ is True
    assert authorization is not None and authorization.allowed is True
    assert assessment.paper_only is True
    assert assessment.human_approval_required is True
    assert assessment.execution_allowed is False


def test_one_leg_long_put_can_pass_and_receive_paper_authorization() -> None:
    request = payload(
        strategy_type="long_put",
        legs=(
            {
                **payload().legs[0].model_dump(),
                "symbol": "AAPL260117P00200000",
            },
        ),
        request_autonomous_paper_authorization=True,
    )
    assessment = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    authorization = build_autonomous_paper_authorization(
        request,
        assessment,
        request_autonomous_paper_authorization=request.request_autonomous_paper_authorization,
        policy_enabled=True,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
    )

    assert assessment.pass_ is True
    assert authorization is not None and authorization.allowed is True


def test_unsupported_leg_counts_fail_strategy_shape() -> None:
    for leg_count in (0, 5):
        values = payload().model_dump()
        values["legs"] = tuple(payload().legs[:1]) * leg_count
        request = StrategyAssessmentRequest.model_construct(
            **values,
        )
        result = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
        assert result.pass_ is False
        assert result.decision == "FAIL"
        assert "strategy_shape" in result.failed_check_codes


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


def test_autonomous_paper_authorization_binds_every_strategy_value() -> None:
    request = payload()
    result = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    authorization = build_autonomous_paper_authorization(
        request,
        result,
        request_autonomous_paper_authorization=True,
        policy_enabled=True,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
        now=datetime.now(UTC),
    )

    assert authorization is not None
    assert authorization.allowed is True
    assert authorization.fingerprint is not None
    assert authorization.strategy_identity == {
        "underlying_symbol": "AAPL",
        "strategy_type": "bull_call_debit_spread",
        "side": "buy",
        "quantity": 1,
        "limit_price": "1.20",
        "max_loss": "40",
        "legs": [
            {
                "symbol": "AAPL260117C00200000",
                "side": "buy",
                "quantity": 1,
                "price": "2.00",
            },
            {
                "symbol": "AAPL260117C00210000",
                "side": "sell",
                "quantity": 1,
                "price": "0.80",
            },
        ],
    }


def test_autonomous_paper_authorization_is_disabled_without_opt_in() -> None:
    request = payload()
    assessment = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    assert build_autonomous_paper_authorization(
        request,
        assessment,
        request_autonomous_paper_authorization=False,
        policy_enabled=True,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
    ) is None


def test_autonomous_paper_authorization_denies_disabled_policy_without_fingerprint() -> None:
    request = payload()
    assessment = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    authorization = build_autonomous_paper_authorization(
        request,
        assessment,
        request_autonomous_paper_authorization=True,
        policy_enabled=False,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
    )
    assert authorization is not None
    assert authorization.allowed is False
    assert authorization.reason == "autonomous_paper_authorization_disabled"
    assert authorization.fingerprint is None
    assert authorization.authorization_id is None


def test_autonomous_paper_authorization_denies_fail_and_invalid_identity() -> None:
    request = payload(max_loss="999999")
    assessment = assess_strategy(request, AssessmentPolicy(), paper_equity=Decimal("10000"))
    denied = build_autonomous_paper_authorization(
        request,
        assessment,
        request_autonomous_paper_authorization=True,
        policy_enabled=True,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
    )
    assert denied is not None
    assert denied.reason == "assessment_not_pass"
    assert denied.fingerprint is None

    missing_workspace = build_autonomous_paper_authorization(
        payload(),
        assess_strategy(payload(), AssessmentPolicy(), paper_equity=Decimal("10000")),
        request_autonomous_paper_authorization=True,
        policy_enabled=True,
        workspace_id=None,
        account_id="paper-account-1",
        guardian_ready=True,
    )
    assert missing_workspace is not None
    assert missing_workspace.reason == "workspace_identity_missing"

    stale_now = datetime.now(UTC)
    stale_request = payload(
        market_evidence_at=stale_now - timedelta(seconds=30),
        observed_at=stale_now - timedelta(seconds=30),
        expires_at=stale_now - timedelta(seconds=1),
    )
    stale_assessment = assess_strategy(
        stale_request,
        AssessmentPolicy(),
        paper_equity=Decimal("10000"),
    )
    stale = build_autonomous_paper_authorization(
        stale_request,
        stale_assessment,
        request_autonomous_paper_authorization=True,
        policy_enabled=True,
        workspace_id="workspace-1",
        account_id="paper-account-1",
        guardian_ready=True,
    )
    assert stale is not None
    assert stale.reason == "assessment_not_pass"

    assessment = assess_strategy(payload(), AssessmentPolicy(), paper_equity=Decimal("10000"))
    for environment, account_id, guardian_ready, reason in (
        ("LIVE", "paper-account-1", True, "paper_environment_required"),
        ("PAPER", None, True, "paper_account_identity_missing"),
        ("PAPER", "paper-account-1", False, "guardian_prerequisites_not_satisfied"),
    ):
        denied = build_autonomous_paper_authorization(
            payload(),
            assessment,
            request_autonomous_paper_authorization=True,
            policy_enabled=True,
            workspace_id="workspace-1",
            environment=environment,
            account_id=account_id,
            guardian_ready=guardian_ready,
        )
        assert denied is not None
        assert denied.reason == reason
        assert denied.fingerprint is None


def test_autonomous_paper_authorization_fingerprint_changes_with_exact_identity() -> None:
    request = payload()

    def issue(candidate: StrategyAssessmentRequest) -> str:
        authorization = build_autonomous_paper_authorization(
            candidate,
            assess_strategy(candidate, AssessmentPolicy(), paper_equity=Decimal("10000")),
            request_autonomous_paper_authorization=True,
            policy_enabled=True,
            workspace_id="workspace-1",
            account_id="paper-account-1",
            guardian_ready=True,
        )
        assert authorization is not None and authorization.fingerprint is not None
        return authorization.fingerprint

    assert issue(request) != issue(
        payload(
            legs=[
                {**request.legs[0].model_dump(), "symbol": "AAPL260117C00190000"},
                request.legs[1].model_dump(),
            ]
        )
    )
    assert issue(request) != issue(payload(limit_price="1.21"))
    assert issue(request) != issue(payload(max_loss="41"))
