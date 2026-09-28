from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import Request

from apps.api.app import create_app
from apps.api.routes import agent
from packages.configuration.settings import Settings
from packages.connected.strategy_assessment import StrategyAssessmentRequest
from packages.domain.broker import BrokerAccount, BrokerSyncStatus
from packages.domain.system import BrokerState
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
