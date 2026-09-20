from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from alpaca.data.enums import DataFeed

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.opportunities import (
    ConnectedOpportunityService,
    _maybe_create_order_intent,
    _scan_disposition,
    _strategy_for_mode,
)
from packages.connected.option_scan_policy import ScanMode
from packages.domain.workflow import CatalystFeatures, Signal
from packages.strategy.catalyst import score_signal


class StockClientStub:
    def __init__(self) -> None:
        self.snapshot_request: Any = None
        self.bars_request: Any = None

    def get_stock_snapshot(self, request: Any) -> dict[str, Any]:
        self.snapshot_request = request
        stock = SimpleNamespace(
            latest_trade=SimpleNamespace(price=101),
            daily_bar=SimpleNamespace(open=100, volume=1000),
            previous_daily_bar=SimpleNamespace(close=99),
        )
        index = SimpleNamespace(
            latest_trade=SimpleNamespace(price=101),
            daily_bar=SimpleNamespace(open=100),
        )
        return {"AAPL": stock, "SPY": index, "QQQ": index}

    def get_stock_bars(self, request: Any) -> Any:
        self.bars_request = request
        return SimpleNamespace(data={"AAPL": [SimpleNamespace(volume=1000)]})


class NewsClientStub:
    def get_news(self, request: Any) -> list[Any]:
        return []


def test_connected_stock_requests_explicitly_use_iex_feed() -> None:
    stock = StockClientStub()
    service = ConnectedOpportunityService.__new__(ConnectedOpportunityService)
    service._stock = stock
    service._news = NewsClientStub()

    service._features("AAPL")

    assert stock.snapshot_request.feed is DataFeed.IEX
    assert stock.bars_request.feed is DataFeed.IEX


def test_non_intent_scan_never_creates_order_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "packages.connected.opportunities.create_order_intent",
        lambda *_args, **_kwargs: pytest.fail("research must not create an order intent"),
    )

    result = _maybe_create_order_intent(
        SimpleNamespace(decision="APPROVE"),
        SimpleNamespace(),
        mode=ScanMode.PRE_SCAN,
        create_intent=True,
    )

    assert result is None


def test_pre_scan_approved_candidate_is_reviewable_without_intent() -> None:
    assert (
        _scan_disposition(
            mode=ScanMode.PRE_SCAN,
            create_intent=True,
            risk_decision="APPROVE",
            intent=None,
        )
        == "PRE_SCAN_CANDIDATE"
    )


def test_pre_scan_without_intent_remains_research_only() -> None:
    assert (
        _scan_disposition(
            mode=ScanMode.PRE_SCAN,
            create_intent=False,
            risk_decision="APPROVE",
            intent=None,
        )
        == "RESEARCH_CANDIDATE"
    )


def test_execution_rejection_is_not_promoted_to_candidate() -> None:
    assert (
        _scan_disposition(
            mode=ScanMode.EXECUTION,
            create_intent=True,
            risk_decision="REJECT",
            intent=None,
        )
        == "RISK_REJECTED"
    )


def test_rejected_risk_with_intent_remains_rejected() -> None:
    assert (
        _scan_disposition(
            mode=ScanMode.EXECUTION,
            create_intent=True,
            risk_decision="REJECT",
            intent=SimpleNamespace(),
        )
        == "RISK_REJECTED"
    )


@pytest.mark.asyncio
async def test_pre_scan_unavailable_candidate_keeps_pre_scan_lifetime() -> None:
    service = ConnectedOpportunityService.__new__(ConnectedOpportunityService)
    persisted: list[Any] = []

    async def persist(result: Any) -> None:
        persisted.append(result)

    service._persist = persist
    observed_at = datetime(2026, 9, 20, 12, tzinfo=UTC)
    features = CatalystFeatures(
        catalyst_confidence=Decimal("0.80"),
        sentiment=Decimal("0"),
        relative_volume=Decimal("1"),
        price_momentum=Decimal("0"),
        gap_percent=Decimal("0"),
        market_confirmation=Decimal("0"),
        sector_confirmation=Decimal("0"),
        liquidity_score=Decimal("0.75"),
    )
    signal = Signal(
        symbol="XYZ",
        observed_at=observed_at,
        features=features,
        score=Decimal("70"),
        source_versions={"market": "test"},
    )

    result = await service._unavailable(
        uuid4(),
        signal,
        SimpleNamespace(model_dump=lambda **_: {"direction": "BULLISH"}),
        observed_at,
        "no_eligible_option_chain",
        mode=ScanMode.PRE_SCAN,
    )

    assert result.expires_at == observed_at + timedelta(hours=18)
    assert persisted == [result]


def test_pre_scan_uses_permissive_underlying_thresholds_but_execution_stays_strict() -> None:
    features = CatalystFeatures(
        catalyst_confidence=Decimal("0.45"),
        sentiment=Decimal("0.80"),
        relative_volume=Decimal("3"),
        price_momentum=Decimal("0.10"),
        gap_percent=Decimal("4"),
        market_confirmation=Decimal("0.30"),
        sector_confirmation=Decimal("0.50"),
        liquidity_score=Decimal("0.90"),
    )
    signal = Signal(
        symbol="XYZ",
        observed_at=datetime(2026, 9, 1, 14, tzinfo=UTC),
        features=features,
        score=score_signal(features),
        source_versions={"market": "test"},
    )
    policy = AssessmentPolicy()

    pre_scan = _strategy_for_mode(ScanMode.PRE_SCAN, policy).evaluate_signal(signal)
    assert pre_scan.signal_id == signal.signal_id
    strict = _strategy_for_mode(ScanMode.EXECUTION, policy).evaluate_signal(signal)
    assert getattr(strict, "reason_codes", ()) == ("weak_catalyst_confidence",)
