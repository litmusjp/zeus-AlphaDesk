from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException

import apps.api.routes.desk as desk_routes
import packages.execution.conditional_exit_runner as exit_runner
from packages.auth.dependencies import AuthPrincipal, WorkspaceContext
from packages.broker.projections import MemoryBrokerProjectionStore
from packages.database.models import ConditionalApprovalRecord
from packages.domain.broker import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPosition,
    BrokerTradeUpdate,
    OrderSubmission,
    ReconciliationSnapshot,
)
from packages.domain.system import BrokerState, TradingEnvironment
from packages.domain.workflow import IntentLeg, OrderIntent, stable_client_order_id
from packages.execution.conditional_approval import ExitOrderSide, order_structure_fingerprint
from packages.execution.engine import ExecutionBlocked, ExecutionEngine, InMemoryIntentStore

NOW = datetime(2026, 9, 20, 14, tzinfo=UTC)


def test_live_close_order_identity_is_bound_to_live_environment() -> None:
    order = SimpleNamespace(
        broker_order_id="broker-1",
        broker_account_id="live-account",
        environment="LIVE",
        client_order_id="client-1",
        symbol="AAPL250117C00100000",
        side="sell",
        asset_class="us_option",
        quantity=Decimal("1"),
        order_type="limit",
        order_class="simple",
        time_in_force="day",
        limit_price=Decimal("1.25"),
    )

    assert exit_runner._close_order_matches(
        order,
        client_order_id="client-1",
        symbol="AAPL250117C00100000",
        order_side=ExitOrderSide.SELL,
        quantity=1,
        expected_limit_price=Decimal("1.25"),
        expected_broker_account_id="live-account",
        expected_environment=TradingEnvironment.LIVE,
    )


class _Begin:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RenewalSession:
    def __init__(self, record: SimpleNamespace) -> None:
        self.record = record

    async def scalar(self, _statement: object) -> SimpleNamespace:
        return self.record

    async def flush(self) -> None:
        return None

    def add(self, _record: object) -> None:
        return None


def _close_renewal_request(record: SimpleNamespace, position: BrokerPosition) -> SimpleNamespace:
    session = _RenewalSession(record)
    database = SimpleNamespace(sessions=SimpleNamespace(begin=lambda: _Begin(session)))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=database)))


def _close_position(*, quantity: str = "1") -> BrokerPosition:
    return BrokerPosition(
        asset_id="asset-1",
        broker_account_id="paper-1",
        environment="PAPER",
        symbol="XYZ260925C00100000",
        asset_class="us_option",
        side="long",
        quantity=Decimal(quantity),
        average_entry_price=Decimal("1.00"),
        cost_basis=Decimal("100"),
        unrealized_pl=Decimal("0"),
        current_price=Decimal("1.50"),
        as_of=NOW,
    )


def _expired_close_record(*, fingerprint: str) -> SimpleNamespace:
    return SimpleNamespace(
        approval_id=UUID("00000000-0000-0000-0000-000000000010"),
        opportunity_id=None,
        approval_kind="CLOSE",
        state="EXPIRED",
        session_date=NOW.date(),
        approved_at=NOW,
        expires_at=NOW,
        client_order_id="ad-exit-original-identity",
        structure_fingerprint=fingerprint,
        max_limit_price=Decimal("0"),
        max_loss=Decimal("0"),
        max_quantity=1,
        max_quote_age_seconds=30,
        min_limit_price=Decimal("1.50"),
        position_asset_id="asset-1",
        position_symbol="XYZ260925C00100000",
        position_side="long",
        exit_order_side="sell",
        broker_order_id=None,
        failure_reason=None,
    )


@pytest.mark.asyncio
async def test_close_approval_renewal_preserves_immutable_client_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = _close_position()
    fingerprint = "EXIT:asset-1:XYZ260925C00100000:long:1"
    record = _expired_close_record(fingerprint=fingerprint)
    request = _close_renewal_request(record, position)

    class CredentialStore:
        async def reveal(self, _workspace_id: UUID, _provider: str) -> dict[str, str]:
            return {"api_key_id": "key", "secret_key": "secret"}

    class Adapter:
        def __init__(self, _key: str, _secret: str) -> None:
            pass

        async def reconcile(self):
            return SimpleNamespace(
                account=SimpleNamespace(account_id="paper-1"),
                positions=(position,),
                open_orders=(),
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(desk_routes, "_credential_store", lambda _request: CredentialStore())
    monkeypatch.setattr(desk_routes, "AlpacaPaperBrokerAdapter", Adapter)
    monkeypatch.setattr(
        desk_routes,
        "_next_session_window",
        lambda _request, _context: _next_window(),
    )

    async def _next_window():
        return NOW.date(), NOW.replace(hour=20)

    result = await desk_routes.approve_position_close_for_next_session(
        "asset-1",
        desk_routes.ConditionalExitApprovalInput(limit_price_bound=Decimal("1.50")),
        request,  # type: ignore[arg-type]
        WorkspaceContext(
            principal=AuthPrincipal(
                user_id=UUID("00000000-0000-0000-0000-000000000011"),
                auth_subject="test",
                email="test@example.test",
                is_admin=False,
            ),
            workspace_id=UUID("00000000-0000-0000-0000-000000000012"),
            status="ACTIVE",
        ),
    )

    assert result.client_order_id == "ad-exit-original-identity"


@pytest.mark.asyncio
async def test_close_approval_renewal_rejects_changed_position_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = _close_position(quantity="2")
    record = _expired_close_record(fingerprint="EXIT:asset-1:XYZ260925C00100000:long:1")
    request = _close_renewal_request(record, position)

    class CredentialStore:
        async def reveal(self, _workspace_id: UUID, _provider: str) -> dict[str, str]:
            return {"api_key_id": "key", "secret_key": "secret"}

    class Adapter:
        def __init__(self, _key: str, _secret: str) -> None:
            pass

        async def reconcile(self):
            return SimpleNamespace(
                account=SimpleNamespace(account_id="paper-1"),
                positions=(position,),
                open_orders=(),
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(desk_routes, "_credential_store", lambda _request: CredentialStore())
    monkeypatch.setattr(desk_routes, "AlpacaPaperBrokerAdapter", Adapter)
    monkeypatch.setattr(
        desk_routes,
        "_next_session_window",
        lambda _request, _context: _next_window(),
    )

    async def _next_window():
        return NOW.date(), NOW.replace(hour=20)

    with pytest.raises(HTTPException, match="new close approval"):
        await desk_routes.approve_position_close_for_next_session(
            "asset-1",
            desk_routes.ConditionalExitApprovalInput(limit_price_bound=Decimal("1.50")),
            request,  # type: ignore[arg-type]
            WorkspaceContext(
                principal=AuthPrincipal(
                    user_id=UUID("00000000-0000-0000-0000-000000000011"),
                    auth_subject="test",
                    email="test@example.test",
                    is_admin=False,
                ),
                workspace_id=UUID("00000000-0000-0000-0000-000000000012"),
                status="ACTIVE",
            ),
        )


def _account(account_id: str = "paper-1") -> BrokerAccount:
    return BrokerAccount(
        account_id=account_id,
        environment="PAPER",
        account_number="PA123",
        status="ACTIVE",
        currency="USD",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
        last_equity=Decimal("100000"),
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        as_of=NOW,
    )


def _order(*, account_id: str = "paper-1", quantity: str = "1") -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-1",
        broker_account_id=account_id,
        environment="PAPER",
        client_order_id="client-1",
        status="done_for_day",
        asset_class="us_option",
        order_type="limit",
        order_class="mleg",
        time_in_force="day",
        quantity=Decimal(quantity),
        filled_quantity=Decimal("0"),
        filled_average_price=None,
        limit_price=Decimal("1.50"),
        created_at=NOW,
        legs=(
            BrokerOrderLeg(
                broker_order_id="broker-1",
                symbol="XYZ260925C00100000",
                side="buy",
                quantity=Decimal(quantity),
                filled_quantity=Decimal("0"),
                status="done_for_day",
            ),
        ),
    )


class StaleFinalFence:
    async def submission_allowed(self) -> tuple[bool, str]:
        return True, ""

    async def authorize_final_submission(self) -> tuple[bool, str]:
        return False, "submission token is stale"


class CountingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def submit_order(self, submission: OrderSubmission) -> BrokerOrder:
        self.calls += 1
        return _order()


def _intent() -> OrderIntent:
    risk_id = UUID("00000000-0000-0000-0000-000000000002")
    legs = (IntentLeg(symbol="XYZ260925C00100000", side="buy", ratio=1),)
    client_order_id = stable_client_order_id(
        risk_id, legs, 1, Decimal("1.50"), "day", "LIMIT_AT_CALCULATED_NET_V1"
    )
    return OrderIntent(
        order_intent_id=UUID("00000000-0000-0000-0000-000000000001"),
        client_order_id=client_order_id,
        risk_decision_id=risk_id,
        quantity=1,
        limit_price=Decimal("1.50"),
        time_in_force="day",
        execution_policy="LIMIT_AT_CALCULATED_NET_V1",
        legs=legs,
        created_at=NOW,
    )


def test_open_approval_renewal_preserves_identity_and_requires_same_structure() -> None:
    intent = _intent()
    record = SimpleNamespace(
        client_order_id="ad-original-open-identity",
        structure_fingerprint=order_structure_fingerprint(intent),
    )

    typed_record = cast(ConditionalApprovalRecord, record)
    desk_routes._validate_open_approval_renewal(typed_record, intent)
    assert record.client_order_id == "ad-original-open-identity"

    changed_intent = intent.model_copy(
        update={"legs": (IntentLeg(symbol="XYZ260925C00105000", side="buy", ratio=1),)}
    )
    with pytest.raises(HTTPException, match="new approval"):
        desk_routes._validate_open_approval_renewal(typed_record, changed_intent)


@pytest.mark.asyncio
async def test_stale_final_submission_token_fails_closed_before_adapter_call() -> None:
    adapter = CountingAdapter()
    engine = ExecutionEngine(
        adapter,
        InMemoryIntentStore(),
        submission_fence=StaleFinalFence(),  # type: ignore[arg-type]
    )

    with pytest.raises(ExecutionBlocked, match="stale"):
        await engine.execute(_intent())

    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_invalid_reconciliation_is_quarantined_and_not_read_back_as_healthy() -> None:
    store = MemoryBrokerProjectionStore()
    invalid = _order(account_id="different-paper-account")

    await store.apply_reconciliation(
        ReconciliationSnapshot(
            account=_account(), positions=(), open_orders=(invalid,), reconciled_at=NOW
        )
    )

    assert (await store.get_status()).state is BrokerState.UNKNOWN
    assert (await store.get_status()).failure_reason == "broker_evidence_invalid"
    assert await store.list_orders() == ()


@pytest.mark.asyncio
async def test_invalid_trade_update_is_quarantined_and_does_not_replace_projection() -> None:
    store = MemoryBrokerProjectionStore()
    await store.apply_reconciliation(
        ReconciliationSnapshot(account=_account(), positions=(), open_orders=(), reconciled_at=NOW)
    )
    invalid = _order(quantity="0")

    await store.apply_trade_update(BrokerTradeUpdate(event="fill", order=invalid, occurred_at=NOW))

    assert (await store.get_status()).state is BrokerState.UNKNOWN
    assert await store.list_orders() == ()


@pytest.mark.asyncio
async def test_invalid_leg_status_is_quarantined_without_replacing_trusted_projection() -> None:
    store = MemoryBrokerProjectionStore()
    trusted = _order()
    await store.apply_reconciliation(
        ReconciliationSnapshot(
            account=_account(), positions=(), open_orders=(trusted,), reconciled_at=NOW
        )
    )
    invalid = trusted.model_copy(
        update={
            "legs": (
                trusted.legs[0].model_copy(
                    update={"status": "filled", "filled_quantity": Decimal("0")}
                ),
            )
        }
    )

    await store.apply_trade_update(BrokerTradeUpdate(event="fill", order=invalid, occurred_at=NOW))

    assert (await store.get_status()).state is BrokerState.UNKNOWN
    assert (await store.list_orders())[0].legs[0].status == "done_for_day"


@pytest.mark.asyncio
async def test_trade_update_cannot_replace_immutable_order_identity() -> None:
    store = MemoryBrokerProjectionStore()
    trusted = _order()
    await store.apply_reconciliation(
        ReconciliationSnapshot(
            account=_account(), positions=(), open_orders=(trusted,), reconciled_at=NOW
        )
    )
    contradictory = trusted.model_copy(update={"client_order_id": "different-client-id"})

    await store.apply_trade_update(
        BrokerTradeUpdate(event="update", order=contradictory, occurred_at=NOW)
    )

    assert (await store.get_status()).state is BrokerState.UNKNOWN
    assert (await store.list_orders())[0].client_order_id == trusted.client_order_id
