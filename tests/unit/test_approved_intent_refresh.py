from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from packages.connected import opportunities
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.opportunities import ConnectedAnalysis, ConnectedOpportunityService
from packages.domain.system import TradingEnvironment
from tests.unit.test_catalyst_workflow import approved_workflow


@pytest.fixture
def refresh_case(monkeypatch: pytest.MonkeyPatch) -> Any:
    candidate, _, intent = approved_workflow()
    now = datetime.now(UTC)
    legs = tuple(
        leg.model_copy(
            update={
                "contract": leg.contract.model_copy(
                    update={
                        "expiration": now.date() + timedelta(days=14),
                        "quote": leg.contract.quote.model_copy(
                            update={
                                "quoted_at": now,
                                "ask": leg.entry_price,
                                "bid": leg.entry_price - Decimal("0.05"),
                            }
                        ),
                    }
                )
            }
        )
        for leg in candidate.structure.legs
    )
    candidate = candidate.model_copy(
        update={"structure": candidate.structure.model_copy(update={"legs": legs})}
    )
    original = ConnectedAnalysis(
        opportunity_id=uuid4(),
        symbol="XYZ",
        disposition="PRE_SCAN_CANDIDATE",
        observed_at=now,
        expires_at=now + timedelta(hours=18),
        signal={},
        candidate=candidate.model_dump(mode="json"),
    )
    service: Any = ConnectedOpportunityService.__new__(ConnectedOpportunityService)
    service._sessions = Any
    service._workspace_id = uuid4()
    service._environment = TradingEnvironment.PAPER
    service._policy = AssessmentPolicy()
    service._persist = AsyncMock()
    service._stock = SimpleNamespace(
        get_stock_snapshot=lambda _: {
            "XYZ": SimpleNamespace(latest_trade=SimpleNamespace(price=102, timestamp=now))
        }
    )
    contracts = tuple(leg.contract for leg in legs)
    # Make executable debit no more than the approved limit.
    contracts = (
        contracts[0],
        contracts[1].model_copy(
            update={"quote": contracts[1].quote.model_copy(update={"bid": Decimal("1.50")})}
        ),
    )
    service._options = SimpleNamespace(
        get_chain_with_diagnostics=AsyncMock(return_value=(contracts, SimpleNamespace()))
    )
    service.analyze = AsyncMock(
        return_value=original.model_copy(update={"disposition": "NO_TRADE"})
    )
    projection = SimpleNamespace(
        get_account=AsyncMock(
            return_value=SimpleNamespace(equity=Decimal("100000"), last_equity=Decimal("100000"))
        ),
        list_positions=AsyncMock(return_value=[]),
    )
    gate = SimpleNamespace(
        evaluate=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="ready"))
    )
    monkeypatch.setattr(opportunities, "PostgresBrokerProjectionStore", lambda *_: projection)
    monkeypatch.setattr(opportunities, "BrokerExecutionGate", lambda *_, **__: gate)
    monkeypatch.setattr(opportunities, "PostgresGuardianStore", lambda *_: None)
    monkeypatch.setattr(
        opportunities,
        "GuardianExecutionGate",
        lambda *_: SimpleNamespace(execution_allowed=AsyncMock(return_value=(True, "normal"))),
    )
    return service, original, intent, contracts, projection, gate


async def test_approved_refresh_ignores_generic_no_trade_and_preserves_intent(
    refresh_case: Any,
) -> None:
    service, original, intent, contracts, _, _ = refresh_case
    result = await service.refresh_approved(original, approved_intent=intent)
    assert result.disposition == "TRADE"
    assert result.order_intent == intent.model_dump(mode="json")
    assert [leg["contract"]["symbol"] for leg in result.candidate["structure"]["legs"]] == [
        contract.symbol for contract in contracts
    ]
    assert result.option_diagnostics["stage"] == "approved_refresh"
    service.analyze.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,reason,retryable",
    [
        ("missing", "approved_contract_data_unavailable", True),
        ("transport", "option_data_unavailable", True),
        ("stale", "stale_quote", True),
        ("structure", "approved_contract_identity_changed", False),
        ("price", "approved_limit_price_exceeded", False),
        ("expiry", "dte_out_of_range", False),
        ("broker", "broker_readiness_unavailable", True),
        ("risk", "risk_rejected", False),
    ],
)
async def test_approved_refresh_classifies_failures(
    refresh_case: Any,
    failure: str,
    reason: str,
    retryable: bool,
) -> None:
    service, original, intent, contracts, projection, gate = refresh_case
    if failure == "missing":
        contracts = contracts[:1]
    if failure == "transport":
        service._options.get_chain_with_diagnostics.side_effect = TimeoutError("secret response")
    if failure in {"stale", "price"}:
        changes = (
            {"quoted_at": datetime.now(UTC) - timedelta(minutes=10)}
            if failure == "stale"
            else {"bid": Decimal("3.10"), "ask": Decimal("3.15")}
        )
        contracts = (
            contracts[0].model_copy(
                update={"quote": contracts[0].quote.model_copy(update=changes)}
            ),
            contracts[1],
        )
    if failure == "structure":
        contracts = (contracts[0].model_copy(update={"strike": Decimal("101")}), contracts[1])
    if failure == "expiry":
        service._policy = AssessmentPolicy(minimum_dte=20)
    if failure == "broker":
        gate.evaluate.return_value = SimpleNamespace(
            allowed=False, reason="Broker reconciliation is stale."
        )
    if failure == "risk":
        projection.get_account.return_value.equity = Decimal("100")
    service._options.get_chain_with_diagnostics.return_value = (contracts, SimpleNamespace())
    result = await service.refresh_approved(original, approved_intent=intent)
    assert result.disposition != "TRADE"
    assert reason in result.reason_codes
    assert result.option_diagnostics["retryable"] is retryable
    assert result.option_diagnostics["stage"]
    assert result.order_intent is None
    assert "secret response" not in result.model_dump_json()


@pytest.mark.parametrize(
    "outcome", ["success", "structure", "stale", "broker", "risk", "guardian", "clock"]
)
async def test_worker_uses_approved_refresh_and_final_gates(
    refresh_case: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    import logging
    from unittest.mock import MagicMock

    from packages.domain.workflow import RankedCandidate
    from packages.execution import conditional_runner as runner
    from packages.execution.conditional_approval import (
        ApprovalState,
        candidate_structure_identity,
        order_structure_fingerprint,
    )
    from packages.execution.engine import ExecutionBlocked
    from tests.unit.test_conditional_approval import broker_order, broker_record, valid_exit_plan

    service, original, intent, _, _, _ = refresh_case
    now = datetime.now(UTC)
    candidate = RankedCandidate.model_validate(original.candidate)
    record = broker_record(
        approved_intent_payload=intent.model_dump(mode="json"),
        approved_structure_identity=candidate_structure_identity(candidate),
        client_order_id=intent.client_order_id,
        expires_at=now + timedelta(hours=1),
        structure_fingerprint=order_structure_fingerprint(intent),
        max_loss=Decimal("150"),
    )
    record.exit_plan_payload = valid_exit_plan(
        opening_approval_id=str(record.approval_id),
        structure_fingerprint=order_structure_fingerprint(intent),
        broker_account_id=record.approved_broker_account_id,
        environment=record.execution_environment,
    )
    fresh = original.model_copy(
        update={
            "disposition": "TRADE",
            "order_intent": intent.model_dump(mode="json"),
            "option_diagnostics": {
                "stage": "approved_refresh",
                "reason": "ready",
                "retryable": False,
            },
        }
    )
    if outcome == "structure":
        changed = candidate.model_copy(
            update={"structure": candidate.structure.model_copy(update={"quantity": 2})}
        )
        fresh = fresh.model_copy(update={"candidate": changed.model_dump(mode="json")})
    if outcome in {"stale", "broker", "risk"}:
        fresh = fresh.model_copy(
            update={
                "disposition": "UNAVAILABLE",
                "order_intent": None,
                "reason_codes": (outcome + "_rejected",),
                "option_diagnostics": {
                    "stage": outcome,
                    "reason": outcome + "_rejected",
                    "retryable": outcome != "risk",
                },
            }
        )
    service.refresh_approved = AsyncMock(return_value=fresh)
    session = AsyncMock()
    session.scalar.side_effect = [
        SimpleNamespace(payload=original.model_dump(mode="json")),
        SimpleNamespace(trading_environment="PAPER", assessment_policy={}),
    ]
    database = MagicMock()
    database.sessions.return_value.__aenter__.return_value = session
    store = AsyncMock()
    store.claim_next.side_effect = [record, None]
    monkeypatch.setattr(runner, "ConditionalApprovalStore", lambda _: store)
    monkeypatch.setattr(runner, "_recover_ready_to_submit", AsyncMock())
    monkeypatch.setattr(
        runner,
        "CredentialStore",
        lambda *_: SimpleNamespace(
            reveal=AsyncMock(return_value={"api_key_id": "fake", "secret_key": "fake"})
        ),
    )
    monkeypatch.setattr(runner, "ConnectedOpportunityService", lambda *_, **__: service)
    clock = SimpleNamespace(is_open=True)
    get_clock = AsyncMock(side_effect=[clock, SimpleNamespace(is_open=outcome != "clock")])
    monkeypatch.setattr(
        runner, "AlpacaMarketClockAdapter", lambda *_, **__: SimpleNamespace(get_clock=get_clock)
    )
    order = broker_order(client_order_id=intent.client_order_id, limit_price=intent.limit_price)
    execute = AsyncMock(return_value=order)
    if outcome == "guardian":
        execute.side_effect = ExecutionBlocked("Guardian halted")
    monkeypatch.setattr(runner, "execute_connected_order", execute)
    with caplog.at_level(logging.INFO):
        await runner.process_workspace_approvals(
            database=database, cipher=MagicMock(), workspace_id=record.workspace_id, now=now
        )
    service.analyze.assert_not_awaited()
    service.refresh_approved.assert_awaited_once()
    if outcome in {"stale", "broker", "clock"}:
        store.release_revalidation.assert_awaited_once()
        store.finish.assert_not_awaited()
    elif outcome in {"structure", "risk", "guardian"}:
        assert store.finish.await_args.kwargs["state"] == ApprovalState.CONDITION_FAILED
    if outcome in {"success", "guardian"}:
        execute.assert_awaited_once()
        store.authorize_submission.assert_awaited_once()
        assert get_clock.await_count == 2
    else:
        execute.assert_not_awaited()
        store.authorize_submission.assert_not_awaited()
    assert any(
        getattr(item, "approval_id", None) == str(record.approval_id)
        and getattr(item, "stage", None)
        and getattr(item, "reason_code", None)
        for item in caplog.records
    )
