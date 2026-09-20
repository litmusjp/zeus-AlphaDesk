from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.broker.validation import (
    validate_broker_account,
    validate_broker_order,
    validate_broker_position,
)
from packages.database.models import (
    BrokerAccountRecord,
    BrokerOrderRecord,
    BrokerPositionRecord,
    BrokerSyncStateRecord,
)
from packages.domain.broker import (
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    BrokerSyncStatus,
    BrokerTradeUpdate,
    ReconciliationSnapshot,
)
from packages.domain.system import BrokerState, TradingEnvironment


class BrokerProjectionStore(Protocol):
    async def mark_state(self, state: BrokerState, failure_reason: str | None = None) -> None: ...

    async def set_stream_connected(self, connected: bool) -> None: ...

    async def apply_reconciliation(self, snapshot: ReconciliationSnapshot) -> int: ...

    async def apply_trade_update(self, update: BrokerTradeUpdate) -> None: ...

    async def get_status(self) -> BrokerSyncStatus: ...

    async def get_account(self) -> BrokerAccount | None: ...

    async def list_positions(self) -> tuple[BrokerPosition, ...]: ...

    async def list_orders(self) -> tuple[BrokerOrder, ...]: ...


def _order_values(order: BrokerOrder) -> dict[str, object]:
    return {
        "broker_order_id": order.broker_order_id,
        "broker_account_id": order.broker_account_id,
        "environment": order.environment,
        "identity_validated_at": order.identity_validated_at,
        "client_order_id": order.client_order_id,
        "status": order.status,
        "asset_class": order.asset_class,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "order_class": order.order_class,
        "time_in_force": order.time_in_force,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "filled_average_price": order.filled_average_price,
        "limit_price": order.limit_price,
        "submitted_at": order.submitted_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "legs": [leg.model_dump(mode="json") for leg in order.legs],
    }


def _snapshot_validation_error(
    snapshot: ReconciliationSnapshot,
    expected_environment: TradingEnvironment = TradingEnvironment.PAPER,
) -> str | None:
    reason = validate_broker_account(snapshot.account, expected_environment)
    if reason is not None:
        return reason
    for position in snapshot.positions:
        reason = validate_broker_position(position, snapshot.account, expected_environment)
        if reason is not None:
            return reason
    for order in snapshot.open_orders:
        reason = validate_broker_order(order, snapshot.account, expected_environment)
        if reason is not None:
            return reason
    return None


class PostgresBrokerProjectionStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        environment: TradingEnvironment = TradingEnvironment.PAPER,
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._environment = environment

    async def _state_for_update(self, session: AsyncSession) -> BrokerSyncStateRecord:
        record = await session.get(BrokerSyncStateRecord, self._workspace_id, with_for_update=True)
        if record is None:
            record = BrokerSyncStateRecord(
                workspace_id=self._workspace_id,
                state=BrokerState.NOT_CONFIGURED.value,
                stream_connected=False,
                generation=0,
                divergence_count=0,
            )
            session.add(record)
            await session.flush()
        return record

    async def mark_state(self, state: BrokerState, failure_reason: str | None = None) -> None:
        async with self._sessions.begin() as session:
            record = await self._state_for_update(session)
            record.state = state.value
            record.failure_reason = failure_reason

    async def set_stream_connected(self, connected: bool) -> None:
        async with self._sessions.begin() as session:
            record = await self._state_for_update(session)
            record.stream_connected = connected
            if not connected and record.state == BrokerState.RECONCILED.value:
                record.state = BrokerState.UNKNOWN.value
                record.failure_reason = "Alpaca trade_updates stream disconnected."

    async def apply_reconciliation(self, snapshot: ReconciliationSnapshot) -> int:
        async with self._sessions.begin() as session:
            state = await self._state_for_update(session)
            validation_error = _snapshot_validation_error(snapshot, self._environment)
            if validation_error is not None:
                state.state = BrokerState.UNKNOWN.value
                state.failure_reason = "broker_evidence_invalid"
                return 0
            current_position_ids = set(
                await session.scalars(
                    select(BrokerPositionRecord.asset_id).where(
                        BrokerPositionRecord.workspace_id == self._workspace_id
                    )
                )
            )
            current_order_ids = set(
                await session.scalars(
                    select(BrokerOrderRecord.broker_order_id).where(
                        BrokerOrderRecord.workspace_id == self._workspace_id
                    )
                )
            )
            broker_position_ids = {position.asset_id for position in snapshot.positions}
            broker_order_ids = {order.broker_order_id for order in snapshot.open_orders}
            divergence_count = len(current_position_ids ^ broker_position_ids) + len(
                current_order_ids ^ broker_order_ids
            )

            await session.execute(
                delete(BrokerAccountRecord).where(
                    BrokerAccountRecord.workspace_id == self._workspace_id
                )
            )
            await session.execute(
                delete(BrokerPositionRecord).where(
                    BrokerPositionRecord.workspace_id == self._workspace_id
                )
            )
            await session.execute(
                delete(BrokerOrderRecord).where(
                    BrokerOrderRecord.workspace_id == self._workspace_id
                )
            )
            account = snapshot.account
            session.add(
                BrokerAccountRecord(
                    workspace_id=self._workspace_id,
                    account_id=account.account_id,
                    environment=account.environment,
                    account_number=account.account_number,
                    status=account.status,
                    currency=account.currency,
                    equity=account.equity,
                    cash=account.cash,
                    buying_power=account.buying_power,
                    options_buying_power=account.options_buying_power,
                    last_equity=account.last_equity,
                    trading_blocked=account.trading_blocked,
                    account_blocked=account.account_blocked,
                    trade_suspended_by_user=account.trade_suspended_by_user,
                    as_of=account.as_of,
                )
            )
            session.add_all(
                BrokerPositionRecord(
                    workspace_id=self._workspace_id,
                    **position.model_copy(
                        update={"identity_validated_at": snapshot.reconciled_at}
                    ).model_dump(),
                )
                for position in snapshot.positions
            )
            session.add_all(
                BrokerOrderRecord(
                    workspace_id=self._workspace_id,
                    **_order_values(
                        order.model_copy(update={"identity_validated_at": snapshot.reconciled_at})
                    ),
                )
                for order in snapshot.open_orders
            )
            state.state = BrokerState.RECONCILED.value
            state.last_reconciled_at = snapshot.reconciled_at
            state.generation += 1
            state.divergence_count += divergence_count
            state.failure_reason = None
            return divergence_count

    async def apply_trade_update(self, update: BrokerTradeUpdate) -> None:
        validated_order = update.order.model_copy(
            update={"identity_validated_at": update.occurred_at}
        )
        values = {"workspace_id": self._workspace_id, **_order_values(validated_order)}
        statement = insert(BrokerOrderRecord).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                BrokerOrderRecord.workspace_id,
                BrokerOrderRecord.broker_order_id,
            ],
            set_={
                key: value
                for key, value in values.items()
                if key not in {"workspace_id", "broker_order_id"}
            },
        )
        async with self._sessions.begin() as session:
            account = await session.scalar(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.workspace_id == self._workspace_id
                )
            )
            if account is None:
                state = await self._state_for_update(session)
                state.state = BrokerState.UNKNOWN.value
                state.failure_reason = "broker_evidence_invalid"
                return
            account_model = BrokerAccount(
                account_id=account.account_id,
                environment=account.environment,
                account_number=account.account_number,
                status=account.status,
                currency=account.currency,
                equity=account.equity,
                cash=account.cash,
                buying_power=account.buying_power,
                options_buying_power=account.options_buying_power,
                last_equity=account.last_equity,
                trading_blocked=account.trading_blocked,
                account_blocked=account.account_blocked,
                trade_suspended_by_user=account.trade_suspended_by_user,
                as_of=account.as_of,
            )
            if validate_broker_order(update.order, account_model, self._environment) is not None:
                state = await self._state_for_update(session)
                state.state = BrokerState.UNKNOWN.value
                state.failure_reason = "broker_evidence_invalid"
                return
            existing = await session.scalar(
                select(BrokerOrderRecord)
                .where(
                    BrokerOrderRecord.workspace_id == self._workspace_id,
                    BrokerOrderRecord.broker_order_id == update.order.broker_order_id,
                )
                .with_for_update()
            )
            if existing is not None and (
                existing.client_order_id != update.order.client_order_id
                or existing.broker_account_id != update.order.broker_account_id
                or existing.environment != update.order.environment
            ):
                state = await self._state_for_update(session)
                state.state = BrokerState.UNKNOWN.value
                state.failure_reason = "broker_evidence_invalid"
                return
            await session.execute(statement)
            state = await self._state_for_update(session)
            state.last_stream_event_at = update.occurred_at
            state.stream_connected = True

    async def get_status(self) -> BrokerSyncStatus:
        async with self._sessions() as session:
            record = await session.get(BrokerSyncStateRecord, self._workspace_id)
            if record is None:
                return BrokerSyncStatus(state=BrokerState.NOT_CONFIGURED)
            return BrokerSyncStatus(
                state=BrokerState(record.state),
                last_reconciled_at=record.last_reconciled_at,
                last_stream_event_at=record.last_stream_event_at,
                stream_connected=record.stream_connected,
                generation=record.generation,
                divergence_count=record.divergence_count,
                failure_reason=record.failure_reason,
            )

    async def get_account(self) -> BrokerAccount | None:
        async with self._sessions() as session:
            record = await session.scalar(
                select(BrokerAccountRecord)
                .where(BrokerAccountRecord.workspace_id == self._workspace_id)
                .limit(1)
            )
            if record is None:
                return None
            return BrokerAccount(
                account_id=record.account_id,
                environment=record.environment,
                account_number=record.account_number,
                status=record.status,
                currency=record.currency,
                equity=record.equity,
                cash=record.cash,
                buying_power=record.buying_power,
                options_buying_power=record.options_buying_power,
                last_equity=record.last_equity,
                trading_blocked=record.trading_blocked,
                account_blocked=record.account_blocked,
                trade_suspended_by_user=record.trade_suspended_by_user,
                as_of=record.as_of,
            )

    async def list_positions(self) -> tuple[BrokerPosition, ...]:
        async with self._sessions() as session:
            records = await session.scalars(
                select(BrokerPositionRecord)
                .where(BrokerPositionRecord.workspace_id == self._workspace_id)
                .order_by(BrokerPositionRecord.symbol)
            )
            return tuple(
                BrokerPosition(
                    asset_id=item.asset_id,
                    broker_account_id=item.broker_account_id,
                    environment=item.environment,
                    identity_validated_at=item.identity_validated_at,
                    symbol=item.symbol,
                    asset_class=item.asset_class,
                    side=item.side,
                    quantity=item.quantity,
                    quantity_available=item.quantity_available,
                    average_entry_price=item.average_entry_price,
                    market_value=item.market_value,
                    cost_basis=item.cost_basis,
                    unrealized_pl=item.unrealized_pl,
                    current_price=item.current_price,
                    as_of=item.as_of,
                )
                for item in records
            )

    async def list_orders(self) -> tuple[BrokerOrder, ...]:
        async with self._sessions() as session:
            records = await session.scalars(
                select(BrokerOrderRecord)
                .where(BrokerOrderRecord.workspace_id == self._workspace_id)
                .order_by(BrokerOrderRecord.created_at.desc())
            )
            return tuple(self._record_to_order(item) for item in records)

    @staticmethod
    def _record_to_order(item: BrokerOrderRecord) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=item.broker_order_id,
            broker_account_id=item.broker_account_id,
            environment=item.environment,
            identity_validated_at=item.identity_validated_at,
            client_order_id=item.client_order_id,
            status=item.status,
            asset_class=item.asset_class,
            symbol=item.symbol,
            side=item.side,
            order_type=item.order_type,
            order_class=item.order_class,
            time_in_force=item.time_in_force,
            quantity=item.quantity,
            filled_quantity=item.filled_quantity,
            filled_average_price=item.filled_average_price,
            limit_price=item.limit_price,
            submitted_at=item.submitted_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            legs=tuple(item.legs),
        )


class MemoryBrokerProjectionStore:
    """Deterministic test double with broker-wins replacement semantics."""

    def __init__(self, environment: TradingEnvironment = TradingEnvironment.PAPER) -> None:
        self.status = BrokerSyncStatus(state=BrokerState.NOT_CONFIGURED)
        self.environment = environment
        self.account: BrokerAccount | None = None
        self.positions: dict[str, BrokerPosition] = {}
        self.orders: dict[str, BrokerOrder] = {}

    async def mark_state(self, state: BrokerState, failure_reason: str | None = None) -> None:
        self.status = self.status.model_copy(
            update={"state": state, "failure_reason": failure_reason}
        )

    async def set_stream_connected(self, connected: bool) -> None:
        next_state = self.status.state if connected else BrokerState.UNKNOWN
        self.status = self.status.model_copy(
            update={"stream_connected": connected, "state": next_state}
        )

    async def apply_reconciliation(self, snapshot: ReconciliationSnapshot) -> int:
        validation_error = _snapshot_validation_error(snapshot, self.environment)
        if validation_error is not None:
            self.status = self.status.model_copy(
                update={"state": BrokerState.UNKNOWN, "failure_reason": "broker_evidence_invalid"}
            )
            return 0
        divergence = len(set(self.positions) ^ {item.asset_id for item in snapshot.positions})
        divergence += len(
            set(self.orders) ^ {item.broker_order_id for item in snapshot.open_orders}
        )
        self.account = snapshot.account
        self.positions = {
            item.asset_id: item.model_copy(update={"identity_validated_at": snapshot.reconciled_at})
            for item in snapshot.positions
        }
        self.orders = {
            item.broker_order_id: item.model_copy(
                update={"identity_validated_at": snapshot.reconciled_at}
            )
            for item in snapshot.open_orders
        }
        self.status = BrokerSyncStatus(
            state=BrokerState.RECONCILED,
            last_reconciled_at=snapshot.reconciled_at,
            stream_connected=self.status.stream_connected,
            generation=self.status.generation + 1,
            divergence_count=self.status.divergence_count + divergence,
        )
        return divergence

    async def apply_trade_update(self, update: BrokerTradeUpdate) -> None:
        if (
            self.account is None
            or validate_broker_order(update.order, self.account, self.environment) is not None
        ):
            self.status = self.status.model_copy(
                update={"state": BrokerState.UNKNOWN, "failure_reason": "broker_evidence_invalid"}
            )
            return
        existing = self.orders.get(update.order.broker_order_id)
        if existing is not None and (
            existing.client_order_id != update.order.client_order_id
            or existing.broker_account_id != update.order.broker_account_id
            or existing.environment != update.order.environment
        ):
            self.status = self.status.model_copy(
                update={"state": BrokerState.UNKNOWN, "failure_reason": "broker_evidence_invalid"}
            )
            return
        self.orders[update.order.broker_order_id] = update.order.model_copy(
            update={"identity_validated_at": update.occurred_at}
        )
        self.status = self.status.model_copy(
            update={"last_stream_event_at": update.occurred_at, "stream_connected": True}
        )

    async def get_status(self) -> BrokerSyncStatus:
        return self.status

    async def get_account(self) -> BrokerAccount | None:
        return self.account

    async def list_positions(self) -> tuple[BrokerPosition, ...]:
        return tuple(self.positions.values())

    async def list_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(self.orders.values())
