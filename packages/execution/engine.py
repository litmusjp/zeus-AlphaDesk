from __future__ import annotations

import asyncio
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from packages.broker.adapter import BrokerAdapter, BrokerPreflightFailed
from packages.domain.broker import BrokerOrder, OrderSubmission, SubmissionLeg
from packages.domain.workflow import OrderIntent
from packages.execution.order_state import BrokerFillState, broker_fill_state


class ExecutionState(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMISSION_STARTED = "SUBMISSION_STARTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    SUBMITTED = "SUBMITTED"


class DuplicateSubmission(RuntimeError):
    pass


class ExecutionBlocked(RuntimeError):
    pass


class SubmissionUncertain(TimeoutError):
    pass


class ExecutionPreflight(Protocol):
    async def execution_allowed(self) -> tuple[bool, str]: ...


class SubmissionFence(Protocol):
    async def submission_allowed(self) -> tuple[bool, str]: ...

    async def authorize_final_submission(self) -> tuple[bool, str]: ...


class IntentStore(Protocol):
    async def reserve_submission(self, intent: OrderIntent) -> bool: ...

    async def release_submission(self, client_order_id: str) -> None: ...

    async def set_state(self, client_order_id: str, state: ExecutionState) -> None: ...

    async def get_state(self, client_order_id: str) -> ExecutionState | None: ...


class InMemoryIntentStore:
    def __init__(self) -> None:
        self.states: dict[str, ExecutionState] = {}
        self._lock = asyncio.Lock()

    async def reserve_submission(self, intent: OrderIntent) -> bool:
        async with self._lock:
            if intent.client_order_id in self.states:
                return False
            self.states[intent.client_order_id] = ExecutionState.SUBMISSION_STARTED
            return True

    async def set_state(self, client_order_id: str, state: ExecutionState) -> None:
        self.states[client_order_id] = state

    async def release_submission(self, client_order_id: str) -> None:
        async with self._lock:
            if self.states.get(client_order_id) is ExecutionState.SUBMISSION_STARTED:
                self.states.pop(client_order_id, None)

    async def get_state(self, client_order_id: str) -> ExecutionState | None:
        return self.states.get(client_order_id)


def _broker_order_matches_intent(
    order: BrokerOrder, intent: OrderIntent, expected_broker_account_id: str | None
) -> bool:
    if expected_broker_account_id is not None and (
        order.broker_account_id != expected_broker_account_id or order.environment != "PAPER"
    ):
        return False
    if (
        not order.broker_order_id
        or order.client_order_id != intent.client_order_id
        or order.asset_class != "us_option"
        or order.order_type.lower() != "limit"
        or order.order_class.lower() != "mleg"
        or order.time_in_force.lower() != "day"
        or order.quantity != Decimal(intent.quantity)
        or order.limit_price != intent.limit_price
        or len(order.legs) != len(intent.legs)
    ):
        return False
    if not all(
        leg.quantity is not None
        and leg.quantity == Decimal(intent_leg.ratio)
        and leg.symbol == intent_leg.symbol
        and leg.side == intent_leg.side
        for leg, intent_leg in zip(order.legs, intent.legs, strict=True)
    ):
        return False
    if not order.filled_quantity.is_finite() or not (
        Decimal("0") <= order.filled_quantity <= Decimal(intent.quantity)
    ):
        return False
    if order.filled_average_price is not None and (
        not order.filled_average_price.is_finite() or order.filled_average_price <= 0
    ):
        return False
    if order.filled_quantity > 0 and order.filled_average_price is None:
        return False
    return (
        broker_fill_state(order.status, order.filled_quantity, Decimal(intent.quantity)) is not None
    )


def _execution_state_for_order(order: BrokerOrder, quantity: int) -> ExecutionState | None:
    state = broker_fill_state(order.status, order.filled_quantity, Decimal(quantity))
    if state is None:
        return None
    if state is BrokerFillState.SUBMITTED and order.status.lower() != "done_for_day":
        return ExecutionState.ACCEPTED
    return {
        BrokerFillState.SUBMITTED: ExecutionState.SUBMITTED,
        BrokerFillState.PARTIALLY_FILLED: ExecutionState.PARTIALLY_FILLED,
        BrokerFillState.FILLED: ExecutionState.FILLED,
    }.get(state)


class ExecutionEngine:
    """The only application service authorized to submit through BrokerAdapter."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        store: IntentStore,
        *,
        preflight: ExecutionPreflight | None = None,
        submission_fence: SubmissionFence | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._preflight = preflight
        self._submission_fence = submission_fence

    async def execute(
        self, intent: OrderIntent, *, expected_broker_account_id: str | None = None
    ) -> BrokerOrder:
        if self._preflight is not None:
            allowed, reason = await self._preflight.execution_allowed()
            if not allowed:
                raise ExecutionBlocked(reason)
        reserved = await self._store.reserve_submission(intent)
        if not reserved:
            raise DuplicateSubmission(f"Intent {intent.client_order_id} was already submitted")
        if self._submission_fence is not None:
            allowed, reason = await self._submission_fence.submission_allowed()
            if not allowed:
                await self._store.release_submission(intent.client_order_id)
                raise ExecutionBlocked(reason)
        submission = OrderSubmission(
            client_order_id=intent.client_order_id,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            legs=tuple(
                SubmissionLeg(symbol=leg.symbol, side=leg.side, ratio=leg.ratio)
                for leg in intent.legs
            ),
        )
        if self._submission_fence is not None:
            allowed, reason = await self._submission_fence.authorize_final_submission()
            if not allowed:
                await self._store.release_submission(intent.client_order_id)
                raise ExecutionBlocked(reason)
        try:
            order = await self._adapter.submit_order(submission)
        except BrokerPreflightFailed as error:
            await self._store.release_submission(intent.client_order_id)
            raise ExecutionBlocked(str(error)) from error
        except Exception as error:
            try:
                await self._store.set_state(
                    intent.client_order_id, ExecutionState.SUBMISSION_UNCERTAIN
                )
            except Exception as state_error:
                raise SubmissionUncertain(
                    "submission uncertainty persistence failed"
                ) from state_error
            raise SubmissionUncertain("broker submission uncertain") from error
        if not _broker_order_matches_intent(order, intent, expected_broker_account_id):
            try:
                await self._store.set_state(
                    intent.client_order_id, ExecutionState.SUBMISSION_UNCERTAIN
                )
            except Exception as state_error:
                raise SubmissionUncertain(
                    "submission uncertainty persistence failed"
                ) from state_error
            raise SubmissionUncertain("broker response did not match approved intent")
        execution_state = _execution_state_for_order(order, intent.quantity)
        if execution_state is None:
            raise SubmissionUncertain("broker response had contradictory status and fill")
        try:
            await self._store.set_state(intent.client_order_id, execution_state)
        except Exception as error:
            raise SubmissionUncertain("post-submit persistence uncertain") from error
        return order

    async def reconcile_uncertain(
        self, intent: OrderIntent, *, expected_broker_account_id: str
    ) -> BrokerOrder | None:
        state = await self._store.get_state(intent.client_order_id)
        if state is not ExecutionState.SUBMISSION_UNCERTAIN:
            raise ValueError("Only uncertain submissions may be reconciled")
        order = await self._adapter.get_order(client_order_id=intent.client_order_id)
        if order is not None:
            if not _broker_order_matches_intent(
                order, intent, expected_broker_account_id=expected_broker_account_id
            ):
                try:
                    await self._store.set_state(
                        intent.client_order_id, ExecutionState.SUBMISSION_UNCERTAIN
                    )
                except Exception as state_error:
                    raise SubmissionUncertain(
                        "submission uncertainty persistence failed"
                    ) from state_error
                raise SubmissionUncertain("recovered broker response did not match intent")
            execution_state = _execution_state_for_order(order, intent.quantity)
            if execution_state is None:
                raise SubmissionUncertain(
                    "recovered broker response had contradictory status and fill"
                )
            await self._store.set_state(intent.client_order_id, execution_state)
        return order
