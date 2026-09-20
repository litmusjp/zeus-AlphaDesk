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
    _pre_scan_reason_codes,
    _scan_disposition,
    _strategy_for_mode,
)
from packages.connected.option_scan_policy import OptionScanDiagnostics, ScanMode
from packages.domain.system import TradingEnvironment
from packages.domain.workflow import CatalystFeatures, Signal
from packages.options.alpaca_adapter import OptionChainFetchDiagnostics
from packages.strategy.catalyst import score_signal
from tests.unit.test_catalyst_workflow import approved_workflow, features


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


def test_execution_intent_uses_risk_evaluated_candidate_quantity() -> None:
    candidate, risk, _ = approved_workflow()
    candidate = candidate.model_copy(
        update={"structure": candidate.structure.model_copy(update={"quantity": 10})}
    )

    intent = _maybe_create_order_intent(
        risk,
        candidate,
        mode=ScanMode.EXECUTION,
        create_intent=True,
    )

    assert intent is not None
    assert intent.quantity == 10


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


def test_pre_scan_reason_codes_reflect_broker_gate() -> None:
    common = {
        "mode": ScanMode.PRE_SCAN,
        "create_intent": True,
        "risk_decision": "APPROVE",
    }

    assert _pre_scan_reason_codes(**common, broker_execution_allowed=True) == (
        "execution_validation_pending",
    )
    assert _pre_scan_reason_codes(**common, broker_execution_allowed=False) == (
        "execution_validation_pending",
        "broker_readiness_deferred",
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
async def test_pre_scan_gate_unavailable_is_candidate_without_order_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, _, _ = approved_workflow()
    service = ConnectedOpportunityService.__new__(ConnectedOpportunityService)
    service._sessions = SimpleNamespace()
    service._workspace_id = uuid4()
    service._policy = AssessmentPolicy()
    service._environment = TradingEnvironment.LIVE
    service._options = SimpleNamespace(
        get_chain_with_diagnostics=lambda _query: None,
    )
    persisted: list[Any] = []

    async def get_chain(_query: Any) -> tuple[list[Any], Any]:
        item = SimpleNamespace(
            expiration=datetime(2026, 9, 25, tzinfo=UTC).date(),
            strike=Decimal("100"),
            contract_id="id-100",
            quote=SimpleNamespace(ask=Decimal("2"), bid=Decimal("1")),
            multiplier=100,
        )
        return [item, item], OptionChainFetchDiagnostics(2, 2, 2, 0, 0)

    async def persist(result: Any) -> None:
        persisted.append(result)

    class ProjectionStore:
        def __init__(self, *_args: Any) -> None:
            assert _args[-1] is TradingEnvironment.LIVE

        async def get_account(self) -> Any:
            return SimpleNamespace(equity=Decimal("100000"), last_equity=Decimal("100000"))

        async def list_positions(self) -> list[Any]:
            return []

    class Gate:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            assert kwargs["environment"] is TradingEnvironment.LIVE

        async def evaluate(self, _now: Any) -> Any:
            return SimpleNamespace(allowed=False, reason="Broker reconciliation is stale.")

    class Strategy:
        def evaluate_signal(self, _signal: Any) -> Any:
            return SimpleNamespace(
                direction="BULLISH",
                model_dump=lambda **_: {"direction": "BULLISH"},
            )

        def rank_candidates(self, _idea: Any, _structures: Any) -> list[Any]:
            return [candidate]

    service._options.get_chain_with_diagnostics = get_chain
    service._persist = persist
    monkeypatch.setattr(
        "packages.connected.opportunities.PostgresBrokerProjectionStore", ProjectionStore
    )
    monkeypatch.setattr("packages.connected.opportunities.BrokerExecutionGate", Gate)
    monkeypatch.setattr(
        "packages.connected.opportunities._strategy_for_mode", lambda *_args: Strategy()
    )
    monkeypatch.setattr(
        service,
        "_features",
        lambda _symbol: (features(), Decimal("100"), datetime(2026, 9, 20, 12, tzinfo=UTC)),
    )
    monkeypatch.setattr(
        "packages.connected.opportunities.select_contracts",
        lambda *_args, **_kwargs: SimpleNamespace(
            selected=[
                SimpleNamespace(
                    expiration=datetime(2026, 9, 25, tzinfo=UTC).date(),
                    strike=Decimal("100"),
                    contract_id="id-100",
                    quote=SimpleNamespace(ask=Decimal("2"), bid=Decimal("1")),
                    multiplier=100,
                ),
                SimpleNamespace(
                    expiration=datetime(2026, 9, 25, tzinfo=UTC).date(),
                    strike=Decimal("105"),
                    contract_id="id-105",
                    quote=SimpleNamespace(ask=Decimal("2"), bid=Decimal("1")),
                    multiplier=100,
                ),
            ],
            diagnostics=OptionScanDiagnostics(2, 2, 0, 2, {}),
        ),
    )
    monkeypatch.setattr(
        "packages.connected.opportunities.build_structure",
        lambda *_args, **_kwargs: candidate.structure,
    )
    monkeypatch.setattr(
        "packages.connected.opportunities.OptionLeg", lambda **kwargs: SimpleNamespace(**kwargs)
    )

    result = await service.analyze("XYZ", mode=ScanMode.PRE_SCAN, create_intent=True)

    assert result.disposition == "PRE_SCAN_CANDIDATE", result.reason_codes
    assert result.reason_codes == (
        "execution_validation_pending",
        "broker_readiness_deferred",
    )
    assert result.order_intent is None
    assert result.risk_decision["decision"] == "APPROVE"
    assert result.risk_decision["checks"][0]["detail"].startswith(
        "Broker execution readiness deferred"
    )
    assert result.option_diagnostics["strict_eligible_contracts"] == 0
    assert persisted == [result]


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
