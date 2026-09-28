from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import Request

from apps.api.app import create_app
from apps.api.routes import agent
from packages.configuration.settings import Settings
from packages.connected.external_account import (
    AlpacaExternalAccountProvider,
    ExternalAccountBinding,
)
from packages.connected.strategy_assessment import StrategyAssessmentRequest
from packages.domain.broker import BrokerAccount, BrokerSyncStatus
from packages.domain.system import BrokerState
from packages.domain.workflow import CatalystFeatures, Signal
from tests.unit.test_strategy_assessment import payload


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, _model: object, _key: object) -> object:
        return SimpleNamespace(assessment_policy={}, updated_at=datetime.now(UTC))


class _Sessions:
    def __call__(self) -> _Session:
        return _Session()


class _Database:
    sessions = _Sessions()


class _OpportunitySession(_Session):
    def __init__(self, opportunity: object) -> None:
        self._opportunity = opportunity

    async def scalar(self, _statement: object) -> object:
        return self._opportunity


class _OpportunitySessions:
    def __init__(self, opportunity: object) -> None:
        self._opportunity = opportunity

    def __call__(self) -> _OpportunitySession:
        return _OpportunitySession(self._opportunity)


class _OpportunityDatabase:
    def __init__(self, opportunity: object) -> None:
        self.sessions = _OpportunitySessions(opportunity)


class _ExternalRequest:
    def __init__(self, database: object, provider: object) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                database=database,
                external_account_provider=provider,
            )
        )


class _Projection:
    def __init__(self, *_args: object) -> None:
        pass

    async def get_status(self) -> BrokerSyncStatus:
        return BrokerSyncStatus(
            state=BrokerState.RECONCILED,
            stream_connected=True,
            last_reconciled_at=datetime.now(UTC),
        )

    async def get_account(self) -> BrokerAccount:
        return BrokerAccount(
            account_id="native-paper",
            environment="PAPER",
            account_number="PA123",
            status="ACTIVE",
            currency="USD",
            equity=10000,
            cash=10000,
            buying_power=10000,
            last_equity=10000,
            trading_blocked=False,
            account_blocked=False,
            trade_suspended_by_user=False,
            as_of=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_route_preserves_native_projection_and_external_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "PostgresBrokerProjectionStore", _Projection)
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=_Database()))),
    )
    key = agent.AgentKeyContext(workspace_id=uuid4(), key_id=uuid4())

    native = await agent.strategy_assessment(payload(), request, key)
    assert native.decision == "PASS"
    assert native.external_identity is None

    native_with_low_caller_score = await agent.strategy_assessment(
        payload(
            market_scanner_features=CatalystFeatures(
                catalyst_confidence="0", sentiment="0", relative_volume="1",
                price_momentum="0", gap_percent="0", market_confirmation="0",
                sector_confirmation="0", liquidity_score="0",
            )
        ),
        request,
        key,
    )
    assert native_with_low_caller_score.decision == "PASS"
    assert native_with_low_caller_score.score_source == "caller_market_scanner_features"

    external_request: StrategyAssessmentRequest = payload(
        external_account_id="op-paper-l1",
        external_sandbox_id="op-sandbox-l1",
        external_environment="PAPER",
    )
    external = await agent.strategy_assessment(external_request, request, key)
    assert external.decision == "UNAVAILABLE"
    assert external.market_scanner_signal_score is None
    assert external.external_identity == {
        "account_id": "op-paper-l1",
        "sandbox_id": "op-sandbox-l1",
        "environment": "PAPER",
    }


def test_strategy_assessment_openapi_schema_keeps_optional_external_contract() -> None:
    schema = create_app(Settings(infrastructure_checks=False)).openapi()
    request_schema = schema["components"]["schemas"]["StrategyAssessmentRequest"]["properties"]
    result_schema = schema["components"]["schemas"]["StrategyAssessmentResult"]["properties"]
    assert (
        "external_account_id"
        not in schema["components"]["schemas"]["StrategyAssessmentRequest"]["required"]
    )
    assert request_schema["external_account_id"]["anyOf"][0]["type"] == "string"
    assert result_schema["external_identity"]["anyOf"]


@pytest.mark.asyncio
async def test_external_route_bound_account_passes_and_cross_key_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    key_l1 = uuid4()
    key_l2 = uuid4()
    now = datetime.now(UTC)
    features = CatalystFeatures(
        catalyst_confidence="1", sentiment="1", relative_volume="4", price_momentum="1",
        gap_percent="0", market_confirmation="1", sector_confirmation="1", liquidity_score="1",
    )
    signal = Signal(
        symbol="AAPL",
        observed_at=now,
        features=features,
        score=95,
        source_versions={"market": "alpaca-real-v1"},
    )
    opportunity = SimpleNamespace(
        payload={"signal": signal.model_dump(mode="json")},
        observed_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    provider = AlpacaExternalAccountProvider(
        (
            ExternalAccountBinding(workspace_id, key_l1, "L1", "sandbox-1", "PAPER", "token"),
        )
    )
    monkeypatch.setattr(
        provider,
        "_get",
        lambda _token: {
            "id": "L1", "status": "ACTIVE", "equity": "10000",
            "trading_blocked": False, "account_blocked": False,
            "trade_suspended_by_user": False,
        },
    )
    request = cast(
        Request,
        _ExternalRequest(_OpportunityDatabase(opportunity), provider),
    )
    external_request = payload(
        external_account_id="L1",
        external_sandbox_id="sandbox-1",
        external_environment="PAPER",
    )
    result = await agent.strategy_assessment(
        external_request, request, agent.AgentKeyContext(workspace_id, key_l1)
    )
    assert result.decision == "PASS"
    assert result.score_source == "alphadesk_connected_opportunity"
    assert result.score_observed_at == now
    assert result.human_approval_required is True
    assert result.execution_allowed is False

    low_features = CatalystFeatures(
        catalyst_confidence="0", sentiment="0", relative_volume="1", price_momentum="0",
        gap_percent="0", market_confirmation="0", sector_confirmation="0", liquidity_score="0",
    )
    low_signal = Signal(
        symbol="AAPL", observed_at=now, features=low_features, score=5,
        source_versions={"market": "alpaca-real-v1"},
    )
    opportunity.payload = {"signal": low_signal.model_dump(mode="json")}
    below_threshold = await agent.strategy_assessment(
        external_request, request, agent.AgentKeyContext(workspace_id, key_l1)
    )
    assert below_threshold.decision == "FAIL"
    assert below_threshold.score_source == "alphadesk_connected_opportunity"
    assert below_threshold.market_scanner_signal_score is not None

    cross_key = await agent.strategy_assessment(
        external_request, request, agent.AgentKeyContext(workspace_id, key_l2)
    )
    assert cross_key.decision == "UNAVAILABLE"
    assert cross_key.market_scanner_signal_score is None
    assert cross_key.score_source is None
    assert cross_key.score_observed_at is None


@pytest.mark.asyncio
async def test_external_route_broker_failure_and_stale_score_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    key_id = uuid4()
    now = datetime.now(UTC)
    features = CatalystFeatures(
        catalyst_confidence="1", sentiment="1", relative_volume="4", price_momentum="1",
        gap_percent="0", market_confirmation="1", sector_confirmation="1", liquidity_score="1",
    )
    old = now - timedelta(hours=1)
    signal = Signal(
        symbol="AAPL", observed_at=old, features=features, score=95,
        source_versions={"market": "alpaca-real-v1"},
    )
    opportunity = SimpleNamespace(
        payload={"signal": signal.model_dump(mode="json")},
        observed_at=old,
        expires_at=old + timedelta(minutes=1),
    )
    provider = AlpacaExternalAccountProvider(
        (ExternalAccountBinding(workspace_id, key_id, "L1", "sandbox-1", "PAPER", "token"),)
    )
    monkeypatch.setattr(provider, "_get", lambda _token: (_ for _ in ()).throw(OSError()))
    request = cast(Request, _ExternalRequest(_OpportunityDatabase(opportunity), provider))
    result = await agent.strategy_assessment(
        payload(
            external_account_id="L1",
            external_sandbox_id="sandbox-1",
            external_environment="PAPER",
        ),
        request,
        agent.AgentKeyContext(workspace_id, key_id),
    )
    assert result.decision == "UNAVAILABLE"
    assert result.market_scanner_signal_score is None
    assert result.score_source is None
    assert result.score_observed_at is None
