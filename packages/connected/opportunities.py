from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from alpaca.data.enums import DataFeed
from alpaca.data.historical import NewsClient, StockHistoricalDataClient
from alpaca.data.requests import NewsRequest, StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.broker.projections import PostgresBrokerProjectionStore
from packages.broker.reconciliation import BrokerExecutionGate
from packages.connected.assessment_policy import AssessmentPolicy, position_exposure
from packages.connected.option_scan_policy import ScanMode, select_contracts
from packages.database.models import ConnectedOpportunityRecord, ConnectedScanRunRecord
from packages.domain.options import LegSide, OptionLeg, OptionType, StructureType
from packages.domain.system import TradingEnvironment
from packages.domain.workflow import CatalystFeatures, NoTrade, OrderIntent, RankedCandidate, Signal
from packages.execution.intents import create_order_intent
from packages.options.alpaca_adapter import (
    AlpacaOptionChainAdapter,
    OptionChainQuery,
)
from packages.options.engine import build_structure
from packages.risk.engine import RiskContext, RiskEngine
from packages.strategy.catalyst import CatalystMomentumStrategy, score_signal

POSITIVE_WORDS = frozenset(
    {"beats", "beat", "raises", "raised", "approval", "approved", "record", "growth", "wins"}
)
NEGATIVE_WORDS = frozenset(
    {"misses", "miss", "cuts", "cut", "lawsuit", "probe", "downgrade", "recall", "warning"}
)
CATALYST_WORDS = (
    POSITIVE_WORDS
    | NEGATIVE_WORDS
    | {
        "earnings",
        "guidance",
        "merger",
        "acquisition",
        "contract",
        "fda",
    }
)


class ConnectedAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: UUID
    scan_run_id: UUID | None = None
    symbol: str
    disposition: str
    source: str = "ALPACA_REAL"
    observed_at: datetime
    expires_at: datetime
    signal: dict[str, Any]
    trade_idea: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    risk_decision: dict[str, Any] | None = None
    order_intent: dict[str, Any] | None = None
    option_diagnostics: dict[str, Any] | None = None
    reason_codes: tuple[str, ...] = ()


def _decimal(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(default if value is None else value))


def _clamp(value: Decimal, low: Decimal = Decimal("-1"), high: Decimal = Decimal("1")) -> Decimal:
    return min(max(value, low), high)


def _news_items(raw: Any) -> list[Any]:
    data = getattr(raw, "data", raw)
    if isinstance(data, dict):
        values: list[Any] = []
        for item in data.values():
            values.extend(item if isinstance(item, list) else [item])
        return values
    return list(data) if isinstance(data, (list, tuple)) else []


def _maybe_create_order_intent(
    risk: Any,
    candidate: RankedCandidate,
    *,
    mode: ScanMode,
    create_intent: bool,
    approved_intent: OrderIntent | None = None,
) -> OrderIntent | None:
    if mode is not ScanMode.EXECUTION or not create_intent or risk.decision != "APPROVE":
        return None
    if approved_intent is not None:
        return approved_intent
    intent = create_order_intent(risk, candidate, quantity=candidate.structure.quantity)
    return intent


def _scan_disposition(
    *, mode: ScanMode, create_intent: bool, risk_decision: str, intent: OrderIntent | None
) -> str:
    if risk_decision != "APPROVE":
        return "RISK_REJECTED"
    if mode is ScanMode.PRE_SCAN and create_intent:
        return "PRE_SCAN_CANDIDATE"
    if intent is not None:
        return "TRADE"
    if not create_intent:
        return "RESEARCH_CANDIDATE"
    return "RISK_REJECTED"


def _pre_scan_reason_codes(
    *, mode: ScanMode, create_intent: bool, risk_decision: str, broker_execution_allowed: bool
) -> tuple[str, ...]:
    if mode is not ScanMode.PRE_SCAN or not create_intent or risk_decision != "APPROVE":
        return ()
    return (
        ("execution_validation_pending",)
        if broker_execution_allowed
        else ("execution_validation_pending", "broker_readiness_deferred")
    )


def _strategy_for_mode(mode: ScanMode, policy: AssessmentPolicy) -> CatalystMomentumStrategy:
    return CatalystMomentumStrategy(
        minimum_score=(
            policy.pre_scan_minimum_signal_score
            if mode is ScanMode.PRE_SCAN
            else policy.minimum_signal_score
        ),
        maximum_gap=policy.maximum_gap_percent,
        minimum_catalyst_confidence=(
            policy.pre_scan_minimum_catalyst_confidence
            if mode is ScanMode.PRE_SCAN
            else policy.minimum_catalyst_confidence
        ),
    )


class ConnectedOpportunityService:
    """Builds Catalyst opportunities exclusively from live Alpaca responses."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        api_key: str,
        secret_key: str,
        policy: AssessmentPolicy | None = None,
        environment: TradingEnvironment = TradingEnvironment.PAPER,
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._stock = StockHistoricalDataClient(api_key, secret_key)
        self._news = NewsClient(api_key, secret_key)
        self._options = AlpacaOptionChainAdapter(api_key, secret_key)
        self._policy = policy or AssessmentPolicy()
        self._environment = environment

    @property
    def policy(self) -> AssessmentPolicy:
        return self._policy

    def _features(self, symbol: str) -> tuple[CatalystFeatures, Decimal, datetime]:
        now = datetime.now(UTC)
        symbols = [symbol, "SPY", "QQQ"]
        # IEX is available to Alpaca paper accounts without a paid SIP subscription.
        # Always select it explicitly: alpaca-py may otherwise request recent SIP data
        # and reject an otherwise valid paper-account credential.
        snapshots = self._stock.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
        )
        stock = snapshots[symbol]
        if (
            stock.latest_trade is None
            or stock.daily_bar is None
            or stock.previous_daily_bar is None
        ):
            raise ValueError("Current and previous market snapshots are required")
        price = _decimal(stock.latest_trade.price)
        daily_open = _decimal(stock.daily_bar.open)
        previous_close = _decimal(stock.previous_daily_bar.close)
        bars = self._stock.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=now - timedelta(days=35),
                end=now,
                limit=24,
                feed=DataFeed.IEX,
            )
        )
        bar_values = list(getattr(bars, "data", {}).get(symbol, []))
        average_volume = sum(
            (_decimal(item.volume) for item in bar_values), Decimal("0")
        ) / Decimal(max(len(bar_values), 1))
        relative_volume = _decimal(stock.daily_bar.volume) / max(average_volume, Decimal("1"))
        news_raw = self._news.get_news(
            NewsRequest(
                symbols=symbol,
                start=now - timedelta(hours=36),
                end=now,
                limit=20,
                include_content=False,
            )
        )
        news = _news_items(news_raw)
        words = " ".join(str(getattr(item, "headline", "")) for item in news).lower().split()
        positive = sum(word.strip(".,:;!?()") in POSITIVE_WORDS for word in words)
        negative = sum(word.strip(".,:;!?()") in NEGATIVE_WORDS for word in words)
        sentiment = _clamp(Decimal(positive - negative) / Decimal(max(positive + negative, 1)))
        catalyst_hits = sum(word.strip(".,:;!?()") in CATALYST_WORDS for word in words)
        catalyst_confidence = min(
            Decimal("1"),
            Decimal("0.15")
            + Decimal(len(news)) * Decimal("0.06")
            + Decimal(catalyst_hits) * Decimal("0.08"),
        )

        def confirmation(index_symbol: str) -> Decimal:
            snapshot = snapshots.get(index_symbol)
            if snapshot is None or snapshot.latest_trade is None or snapshot.daily_bar is None:
                return Decimal("0")
            index_open = _decimal(snapshot.daily_bar.open)
            index_price = _decimal(snapshot.latest_trade.price)
            return _clamp((index_price / max(index_open, Decimal("0.01")) - 1) * Decimal("20"))

        features = CatalystFeatures(
            catalyst_confidence=catalyst_confidence,
            sentiment=sentiment,
            relative_volume=max(relative_volume, Decimal("0")),
            price_momentum=_clamp((price / max(daily_open, Decimal("0.01")) - 1) * Decimal("20")),
            gap_percent=(daily_open / max(previous_close, Decimal("0.01")) - 1) * Decimal("100"),
            market_confirmation=confirmation("SPY"),
            sector_confirmation=confirmation("QQQ"),
            liquidity_score=Decimal("0.75"),
        )
        return features, price, now

    async def analyze(
        self,
        symbol: str,
        *,
        scan_run_id: UUID | None = None,
        mode: ScanMode = ScanMode.EXECUTION,
        create_intent: bool = True,
        approved_intent: OrderIntent | None = None,
    ) -> ConnectedAnalysis:
        normalized = symbol.strip().upper()
        if not normalized.isalnum() or len(normalized) > 16:
            raise ValueError("Invalid symbol")
        features, underlying_price, now = await asyncio.to_thread(self._features, normalized)
        signal = Signal(
            symbol=normalized,
            observed_at=now,
            features=features,
            score=score_signal(features),
            source_versions={
                "market": "alpaca-real-v1",
                "news": "alpaca-real-v1",
                "options": "alpaca-real-v1",
            },
        )
        strategy = _strategy_for_mode(mode, self._policy)
        idea = strategy.evaluate_signal(signal)
        opportunity_id = uuid4()
        expires_at = now + (
            timedelta(hours=18) if mode is ScanMode.PRE_SCAN else timedelta(minutes=2)
        )
        if isinstance(idea, NoTrade):
            result = ConnectedAnalysis(
                opportunity_id=opportunity_id,
                scan_run_id=scan_run_id,
                symbol=normalized,
                disposition="NO_TRADE",
                observed_at=now,
                expires_at=expires_at,
                signal=signal.model_dump(mode="json"),
                reason_codes=idea.reason_codes,
            )
            await self._persist(result)
            return result

        contracts, fetch_diagnostics = await self._options.get_chain_with_diagnostics(
            OptionChainQuery(
                underlying_symbol=normalized,
                expiration_date_gte=date.today() + timedelta(days=self._policy.minimum_dte),
                expiration_date_lte=date.today() + timedelta(days=self._policy.maximum_dte),
                strike_price_gte=underlying_price
                * (Decimal("1") - self._policy.maximum_strike_distance_ratio),
                strike_price_lte=underlying_price
                * (Decimal("1") + self._policy.maximum_strike_distance_ratio),
            )
        )
        wanted_type = OptionType.CALL if idea.direction == "BULLISH" else OptionType.PUT
        selection = select_contracts(
            tuple(contracts),
            underlying_price=underlying_price,
            wanted_type=wanted_type,
            as_of=now,
            mode=mode,
            policy=self._policy,
        )
        eligible = list(selection.selected)
        option_diagnostics = {
            **asdict(fetch_diagnostics),
            **asdict(selection.diagnostics),
        }
        expirations = sorted({item.expiration for item in eligible})
        if not expirations:
            return await self._unavailable(
                opportunity_id,
                signal,
                idea,
                now,
                "no_eligible_option_chain",
                scan_run_id=scan_run_id,
                option_diagnostics=option_diagnostics,
                mode=mode,
            )
        selected = sorted(
            (item for item in eligible if item.expiration == expirations[0]),
            key=lambda item: item.strike,
        )
        if len(selected) < 2:
            return await self._unavailable(
                opportunity_id,
                signal,
                idea,
                now,
                "insufficient_vertical_legs",
                scan_run_id=scan_run_id,
                option_diagnostics=option_diagnostics,
                mode=mode,
            )
        if wanted_type is OptionType.CALL:
            long_index = min(
                range(len(selected)), key=lambda i: abs(selected[i].strike - underlying_price)
            )
            if long_index == len(selected) - 1:
                long_index -= 1
            long_contract, short_contract = selected[long_index], selected[long_index + 1]
            structure_type = StructureType.BULL_CALL_DEBIT_SPREAD
        else:
            long_index = min(
                range(len(selected)), key=lambda i: abs(selected[i].strike - underlying_price)
            )
            if long_index == 0:
                long_index = 1
            long_contract, short_contract = selected[long_index], selected[long_index - 1]
            structure_type = StructureType.BEAR_PUT_DEBIT_SPREAD
        try:
            debit_per_share = long_contract.quote.ask - short_contract.quote.bid
            one_contract_cost = debit_per_share * Decimal(long_contract.multiplier)
            quantity = min(
                self._policy.maximum_contracts_per_candidate,
                int(
                    self._policy.max_investment_per_candidate
                    / max(one_contract_cost, Decimal("0.01"))
                ),
            )
            if quantity < 1:
                return await self._unavailable(
                    opportunity_id,
                    signal,
                    idea,
                    now,
                    "investment_cap_below_one_contract",
                    scan_run_id=scan_run_id,
                    option_diagnostics=option_diagnostics,
                    mode=mode,
                )
            structure = build_structure(
                structure_type,
                (
                    OptionLeg(
                        side=LegSide.LONG,
                        contract=long_contract,
                        entry_price=long_contract.quote.ask,
                    ),
                    OptionLeg(
                        side=LegSide.SHORT,
                        contract=short_contract,
                        entry_price=short_contract.quote.bid,
                    ),
                ),
                quantity=quantity,
            )
        except ValueError:
            return await self._unavailable(
                opportunity_id,
                signal,
                idea,
                now,
                "invalid_option_structure",
                scan_run_id=scan_run_id,
                option_diagnostics=option_diagnostics,
                mode=mode,
            )
        strict_selection = select_contracts(
            tuple(contracts),
            underlying_price=underlying_price,
            wanted_type=wanted_type,
            as_of=now,
            mode=ScanMode.EXECUTION,
            policy=self._policy,
        )
        strict_contract_ids = {item.contract_id for item in strict_selection.selected}
        option_diagnostics["selected_structure_strictly_eligible"] = int(
            long_contract.contract_id in strict_contract_ids
            and short_contract.contract_id in strict_contract_ids
        )
        candidate = strategy.rank_candidates(idea, (structure,))[0]
        projections = PostgresBrokerProjectionStore(
            self._sessions, self._workspace_id, self._environment
        )
        account = await projections.get_account()
        gate = await BrokerExecutionGate(
            projections, environment=self._environment
        ).evaluate(now)
        if account is None:
            return await self._unavailable(
                opportunity_id,
                signal,
                idea,
                now,
                "broker_account_unavailable",
                scan_run_id=scan_run_id,
                mode=mode,
            )
        positions = await projections.list_positions()
        open_planned_loss, underlying_open_risk, portfolio_greeks_available = position_exposure(
            positions, normalized, account.equity
        )
        risk = RiskEngine(self._policy.as_risk_policy()).evaluate(
            candidate,
            RiskContext(
                paper_equity=account.equity,
                open_planned_loss=open_planned_loss,
                underlying_open_risk=underlying_open_risk,
                daily_loss=max(account.last_equity - account.equity, Decimal("0")),
                drawdown_percent=(
                    max(account.last_equity - account.equity, Decimal("0"))
                    / max(account.last_equity, Decimal("1"))
                    * Decimal("100")
                ),
                concurrent_option_structures=sum(
                    1 for position in positions if position.asset_class.lower() == "us_option"
                ),
                broker_execution_allowed=gate.allowed,
                broker_state_required=mode is ScanMode.EXECUTION,
                broker_execution_reason=gate.reason,
                portfolio_greeks_available=portfolio_greeks_available,
            ),
        )
        intent = _maybe_create_order_intent(
            risk,
            candidate,
            mode=mode,
            create_intent=create_intent,
            approved_intent=approved_intent,
        )
        result = ConnectedAnalysis(
            opportunity_id=opportunity_id,
            scan_run_id=scan_run_id,
            symbol=normalized,
            disposition=_scan_disposition(
                mode=mode,
                create_intent=create_intent,
                risk_decision=risk.decision,
                intent=intent,
            ),
            observed_at=now,
            expires_at=expires_at,
            signal=signal.model_dump(mode="json"),
            trade_idea=idea.model_dump(mode="json"),
            candidate=candidate.model_dump(mode="json"),
            risk_decision=risk.model_dump(mode="json"),
            order_intent=None if intent is None else intent.model_dump(mode="json"),
            option_diagnostics=option_diagnostics,
            reason_codes=_pre_scan_reason_codes(
                mode=mode,
                create_intent=create_intent,
                risk_decision=risk.decision,
                broker_execution_allowed=gate.allowed,
            ),
        )
        await self._persist(result)
        return result

    async def _unavailable(
        self,
        opportunity_id: UUID,
        signal: Signal,
        idea: Any,
        now: datetime,
        reason: str,
        *,
        scan_run_id: UUID | None = None,
        option_diagnostics: dict[str, Any] | None = None,
        mode: ScanMode = ScanMode.EXECUTION,
    ) -> ConnectedAnalysis:
        result = ConnectedAnalysis(
            opportunity_id=opportunity_id,
            scan_run_id=scan_run_id,
            symbol=signal.symbol,
            disposition="UNAVAILABLE",
            observed_at=now,
            expires_at=now
            + (timedelta(hours=18) if mode is ScanMode.PRE_SCAN else timedelta(minutes=2)),
            signal=signal.model_dump(mode="json"),
            trade_idea=idea.model_dump(mode="json"),
            option_diagnostics=option_diagnostics,
            reason_codes=(reason,),
        )
        await self._persist(result)
        return result

    async def _persist(self, result: ConnectedAnalysis) -> None:
        async with self._sessions.begin() as session:
            session.add(
                ConnectedOpportunityRecord(
                    opportunity_id=result.opportunity_id,
                    workspace_id=self._workspace_id,
                    scan_run_id=result.scan_run_id,
                    symbol=result.symbol,
                    state=result.disposition,
                    source="ALPACA_REAL",
                    payload=result.model_dump(mode="json"),
                    observed_at=result.observed_at,
                    expires_at=result.expires_at,
                    created_at=datetime.now(UTC),
                )
            )


async def start_scan_run(
    sessions: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    *,
    trigger: str,
    attempted_count: int,
) -> ConnectedScanRunRecord:
    record = ConnectedScanRunRecord(
        scan_run_id=uuid4(),
        workspace_id=workspace_id,
        trigger=trigger,
        source="ALPACA_REAL",
        started_at=datetime.now(UTC),
        completed_at=None,
        attempted_count=attempted_count,
        completed_count=0,
        failed_count=0,
    )
    async with sessions.begin() as session:
        session.add(record)
    return record


async def complete_scan_run(
    sessions: async_sessionmaker[AsyncSession],
    scan_run_id: UUID,
    *,
    completed_count: int,
    failed_count: int,
) -> ConnectedScanRunRecord:
    async with sessions.begin() as session:
        record = await session.get(ConnectedScanRunRecord, scan_run_id, with_for_update=True)
        if record is None:
            raise RuntimeError("Connected scan run disappeared before completion")
        record.completed_at = datetime.now(UTC)
        record.completed_count = completed_count
        record.failed_count = failed_count
    return record
