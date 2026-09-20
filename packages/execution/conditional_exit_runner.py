from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from math import ceil
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from packages.broker.adapter import BrokerPreflightFailed
from packages.broker.alpaca_adapter import AlpacaBrokerAdapter
from packages.connected.market_clock import AlpacaMarketClockAdapter
from packages.database.models import ConditionalApprovalRecord, WorkspaceRecord
from packages.database.session import Database
from packages.domain.system import TradingEnvironment
from packages.execution.conditional_approval import (
    ApprovalState,
    ConditionalExitApproval,
    ExitOrderSide,
    RevalidationDecision,
    revalidate_exit_for_submission,
)
from packages.execution.conditional_store import ConditionalApprovalStore
from packages.execution.order_state import BrokerFillState, broker_fill_state
from packages.guardian.gate import GuardianExecutionGate
from packages.guardian.store import PostgresGuardianStore
from packages.options.alpaca_adapter import AlpacaOptionChainAdapter
from packages.security.credentials import CredentialCipher
from packages.security.store import CredentialStore


class PreSubmissionCheckFailed(RuntimeError):
    pass


class SubmissionAttempted(RuntimeError):
    pass


def _broker_order_state(
    status: str, filled_quantity: Decimal = Decimal("0"), quantity: Decimal | None = None
) -> ApprovalState:
    state = broker_fill_state(status, filled_quantity, quantity or Decimal("0"))
    if state is None:
        return ApprovalState.SUBMISSION_UNCERTAIN
    return {
        BrokerFillState.SUBMITTED: ApprovalState.SUBMITTED,
        BrokerFillState.PARTIALLY_FILLED: ApprovalState.PARTIALLY_FILLED,
        BrokerFillState.FILLED: ApprovalState.FILLED,
        BrokerFillState.BROKER_REJECTED: ApprovalState.BROKER_REJECTED,
    }.get(state, ApprovalState.SUBMISSION_UNCERTAIN)


def _close_order_matches(
    order: object,
    *,
    client_order_id: str,
    symbol: str,
    order_side: ExitOrderSide,
    quantity: int,
    expected_limit_price: Decimal | None = None,
    limit_price_bound: Decimal | None = None,
    expected_broker_account_id: str | None = None,
    expected_environment: TradingEnvironment = TradingEnvironment.PAPER,
) -> bool:
    order_limit = getattr(order, "limit_price", None)
    if order_limit is None or not order_limit.is_finite() or order_limit <= 0:
        return False
    return (
        bool(getattr(order, "broker_order_id", None))
        and (
            expected_broker_account_id is None
            or (
                getattr(order, "broker_account_id", None) == expected_broker_account_id
                and getattr(order, "environment", None) == expected_environment.value
            )
        )
        and getattr(order, "client_order_id", None) == client_order_id
        and getattr(order, "symbol", None) == symbol
        and getattr(order, "side", None) == order_side.value
        and getattr(order, "asset_class", None) == "us_option"
        and getattr(order, "quantity", None) == Decimal(quantity)
        and str(getattr(order, "order_type", "")).lower() == "limit"
        and str(getattr(order, "order_class", "")).lower() == "simple"
        and str(getattr(order, "time_in_force", "")).lower() == "day"
        and (
            (expected_limit_price is not None and order_limit == expected_limit_price)
            or (
                expected_limit_price is None
                and limit_price_bound is not None
                and (
                    (order_side is ExitOrderSide.SELL and order_limit >= limit_price_bound)
                    or (order_side is ExitOrderSide.BUY and order_limit <= limit_price_bound)
                )
            )
        )
    )


def _close_fill_response_is_valid(order: object, quantity: int) -> bool:
    filled = getattr(order, "filled_quantity", None)
    if filled is None:
        return False
    try:
        filled_decimal = Decimal(str(filled))
    except Exception:
        return False
    if not filled_decimal.is_finite() or filled_decimal < 0 or filled_decimal > Decimal(quantity):
        return False
    average_price = getattr(order, "filled_average_price", None)
    if average_price is not None:
        try:
            average_decimal = Decimal(str(average_price))
        except Exception:
            return False
        if not average_decimal.is_finite() or average_decimal <= 0:
            return False
    if filled_decimal > 0:
        average_price = getattr(order, "filled_average_price", None)
        if average_price is None:
            return False
        try:
            average_decimal = Decimal(str(average_price))
        except Exception:
            return False
        if not average_decimal.is_finite() or average_decimal <= 0:
            return False
    status = str(getattr(order, "status", "")).lower()
    if status not in {
        "new",
        "accepted",
        "pending_new",
        "done_for_day",
        "partially_filled",
        "filled",
        "rejected",
        "canceled",
        "expired",
        "replaced",
    }:
        return False
    if status in {"new", "accepted", "pending_new"} and filled_decimal != 0:
        return False
    return broker_fill_state(status, filled_decimal, Decimal(quantity)) is not None


async def process_workspace_exit_approvals(
    *, database: Database, cipher: CredentialCipher, workspace_id: UUID, now: datetime
) -> None:
    store = ConditionalApprovalStore(database)
    now = datetime.now(UTC)
    await store.reclaim_stale_revalidating(workspace_id=workspace_id, now=now)
    await _recover_ready_to_submit(database, store, cipher, workspace_id)
    await store.expire_before(workspace_id=workspace_id, now=now)
    session_date = now.astimezone(ZoneInfo("America/New_York")).date()
    async with database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, workspace_id)
    if workspace is None:
        return
    environment = TradingEnvironment(workspace.trading_environment)
    provider = "ALPACA_LIVE" if environment is TradingEnvironment.LIVE else "ALPACA_PAPER"
    secret = await CredentialStore(database.sessions, cipher).reveal(workspace_id, provider)
    if secret is None:
        return

    while True:
        now = datetime.now(UTC)
        session_date = now.astimezone(ZoneInfo("America/New_York")).date()
        record = await store.claim_next(
            workspace_id=workspace_id,
            session_date=session_date,
            now=now,
            approval_kind="CLOSE",
        )
        if record is None:
            return
        claim_token = record.claim_token
        if claim_token is None:
            await store.mark_unclaimable_recovery(
                workspace_id=workspace_id,
                approval_id=record.approval_id,
                now=now,
            )
            continue
        bound_claim_token = claim_token

        async def _finish(
            approval_id: UUID, _claim_token: UUID = bound_claim_token, **kwargs: Any
        ) -> None:
            await store.finish(
                approval_id,
                workspace_id=workspace_id,
                claim_token=_claim_token,
                **kwargs,
            )

        submission_started = [False]
        try:
            await _process_claimed_exit(
                database=database,
                store=store,
                secret=secret,
                record=record,
                workspace_id=workspace_id,
                environment=environment,
                session_date=session_date,
                now=now,
                submission_started=submission_started,
            )
        except PreSubmissionCheckFailed as error:
            await _finish(
                record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=datetime.now(UTC),
                reason=str(error),
            )
        except SubmissionAttempted as error:
            await _finish(
                record.approval_id,
                state=ApprovalState.SUBMISSION_UNCERTAIN,
                now=now,
                reason=str(error),
            )
        except Exception as error:
            await _finish(
                record.approval_id,
                state=(
                    ApprovalState.SUBMISSION_UNCERTAIN
                    if submission_started[0]
                    else ApprovalState.CONDITION_FAILED
                ),
                now=now,
                reason=type(error).__name__,
            )


async def _process_claimed_exit(
    *,
    database: Database,
    store: ConditionalApprovalStore,
    secret: dict[str, object],
    record: ConditionalApprovalRecord,
    workspace_id: UUID,
    environment: TradingEnvironment,
    session_date: date,
    now: datetime,
    submission_started: list[bool],
) -> None:
    claim_token = record.claim_token
    if claim_token is None:
        return
    bound_claim_token = claim_token

    async def _finish(
        approval_id: UUID, _claim_token: UUID = bound_claim_token, **kwargs: Any
    ) -> None:
        await store.finish(
            approval_id,
            workspace_id=workspace_id,
            claim_token=_claim_token,
            **kwargs,
        )

    if (
        record.position_asset_id is None
        or record.position_symbol is None
        or record.position_side is None
        or record.exit_order_side is None
    ):
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="exit_identity_incomplete",
        )
        return
    try:
        order_side = ExitOrderSide(record.exit_order_side)
    except ValueError:
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="exit_side_invalid",
        )
        return
    bound = record.min_limit_price if order_side is ExitOrderSide.SELL else record.max_limit_price
    if bound is None or bound <= 0:
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="exit_limit_bound_missing",
        )
        return

    broker = AlpacaBrokerAdapter(
        str(secret["api_key_id"]), str(secret["secret_key"]), environment=environment
    )
    try:
        try:
            existing = await broker.get_order(client_order_id=record.client_order_id)
            if existing is not None:
                if not _close_order_matches(
                    existing,
                    client_order_id=record.client_order_id,
                    symbol=record.position_symbol,
                    order_side=order_side,
                    quantity=record.max_quantity,
                    limit_price_bound=bound,
                    expected_broker_account_id=record.approved_broker_account_id,
                    expected_environment=environment,
                ):
                    await _finish(
                        record.approval_id,
                        state=ApprovalState.SUBMISSION_UNCERTAIN,
                        now=datetime.now(UTC),
                        reason="broker_order_identity_mismatch",
                        broker_order_id=existing.broker_order_id or None,
                        broker_order=existing,
                    )
                else:
                    if not _close_fill_response_is_valid(existing, record.max_quantity):
                        await _finish(
                            record.approval_id,
                            state=ApprovalState.SUBMISSION_UNCERTAIN,
                            now=now,
                            reason="recovered_broker_fill_response_invalid",
                            broker_order_id=existing.broker_order_id or None,
                            broker_order=existing,
                        )
                        return
                    await _finish(
                        record.approval_id,
                        state=_broker_order_state(
                            existing.status, existing.filled_quantity, Decimal(record.max_quantity)
                        ),
                        now=datetime.now(UTC),
                        submitted_at=existing.submitted_at or now,
                        broker_order_id=existing.broker_order_id,
                        broker_order=existing,
                        reason=(
                            existing.status
                            if _broker_order_state(
                                existing.status,
                                existing.filled_quantity,
                                Decimal(record.max_quantity),
                            )
                            is ApprovalState.BROKER_REJECTED
                            else None
                        ),
                    )
                return
            snapshot = await broker.reconcile()
        except Exception as error:
            raise PreSubmissionCheckFailed("initial_broker_check_failed") from error
    finally:
        await broker.close()

    account = snapshot.account
    if (
        account.status.upper() != "ACTIVE"
        or account.trading_blocked
        or account.account_blocked
        or account.trade_suspended_by_user
    ):
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="broker_account_unavailable",
        )
        return
    allowed, guardian_reason = await GuardianExecutionGate(
        PostgresGuardianStore(database.sessions, workspace_id)
    ).execution_allowed()
    if not allowed:
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason=f"guardian_blocked:{guardian_reason}",
        )
        return

    position = next(
        (
            item
            for item in snapshot.positions
            if item.asset_id == record.position_asset_id and item.symbol == record.position_symbol
        ),
        None,
    )
    if position is None or position.asset_class.lower() != "us_option":
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="option_position_required",
        )
        return

    option_data = AlpacaOptionChainAdapter(str(secret["api_key_id"]), str(secret["secret_key"]))
    try:
        quote = await option_data.get_latest_quote(record.position_symbol)
    except Exception as error:
        raise PreSubmissionCheckFailed("option_quote_check_failed") from error
    quoted_at = quote.quoted_at
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if quoted_at > now:
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="quote_timestamp_in_future",
        )
        return
    quote_age_seconds = max(0, ceil((now - quoted_at).total_seconds()))
    limit_price = quote.bid if order_side is ExitOrderSide.SELL else quote.ask
    if not limit_price.is_finite() or limit_price <= 0:
        await _finish(
            record.approval_id,
            state=ApprovalState.CONDITION_FAILED,
            now=now,
            reason="quote_not_executable",
        )
        return

    now = datetime.now(UTC)
    session_date = now.astimezone(ZoneInfo("America/New_York")).date()
    approval = ConditionalExitApproval(
        approval_id=record.approval_id,
        workspace_id=record.workspace_id,
        position_asset_id=record.position_asset_id,
        position_symbol=record.position_symbol,
        position_side=record.position_side,
        approved_quantity=Decimal(record.max_quantity),
        order_side=order_side,
        session_date=record.session_date,
        approved_at=record.approved_at,
        expires_at=record.expires_at,
        client_order_id=record.client_order_id,
        limit_price_bound=bound,
        max_quote_age_seconds=record.max_quote_age_seconds,
        state=ApprovalState.APPROVED_FOR_SESSION,
    )
    result = revalidate_exit_for_submission(
        approval,
        now=now,
        session_date=session_date,
        position_asset_id=position.asset_id,
        position_symbol=position.symbol,
        position_side=position.side,
        current_quantity=position.quantity,
        order_side=order_side,
        limit_price=limit_price,
        quote_age_seconds=quote_age_seconds,
    )
    if result.decision is not RevalidationDecision.READY_TO_SUBMIT:
        await _finish(
            record.approval_id,
            state=(
                ApprovalState.EXPIRED
                if result.decision is RevalidationDecision.EXPIRED
                else ApprovalState.CONDITION_FAILED
            ),
            now=now,
            reason=result.reason,
        )
        return

    broker = AlpacaBrokerAdapter(
        str(secret["api_key_id"]), str(secret["secret_key"]), environment=environment
    )
    order = None
    try:
        try:
            final_snapshot = await broker.reconcile()
        except Exception as error:
            raise PreSubmissionCheckFailed("final_broker_check_failed") from error
        final_position = next(
            (
                item
                for item in final_snapshot.positions
                if item.asset_id == record.position_asset_id
                and item.symbol == record.position_symbol
                and item.side == record.position_side
            ),
            None,
        )
        terminal_order_statuses = {"filled", "canceled", "expired", "rejected", "replaced"}
        if (
            record.approved_broker_account_id is None
            or final_snapshot.account.environment != environment.value
            or final_snapshot.account.account_id != record.approved_broker_account_id
            or final_position is None
            or final_position.asset_class.lower() != "us_option"
            or final_position.quantity != Decimal(record.max_quantity)
            or final_position.quantity_available is None
            or final_position.quantity_available < Decimal(record.max_quantity)
            or any(
                item.symbol == record.position_symbol
                and item.side == order_side.value
                and item.status.lower() not in terminal_order_statuses
                for item in final_snapshot.open_orders
            )
        ):
            await _finish(
                record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=datetime.now(UTC),
                reason="close_position_or_order_changed",
            )
            return
        try:
            final_quote = await option_data.get_latest_quote(record.position_symbol)
        except Exception as error:
            raise PreSubmissionCheckFailed("final_option_quote_check_failed") from error
        final_quoted_at = final_quote.quoted_at
        if final_quoted_at.tzinfo is None:
            final_quoted_at = final_quoted_at.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if final_quoted_at > now:
            await _finish(
                record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=now,
                reason="final_quote_timestamp_in_future",
            )
            return
        final_quote_age_seconds = max(0, ceil((now - final_quoted_at).total_seconds()))
        final_limit_price = final_quote.bid if order_side is ExitOrderSide.SELL else final_quote.ask
        if not final_limit_price.is_finite() or final_limit_price <= 0:
            await _finish(
                record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=now,
                reason="invalid_limit_price",
            )
            return
        final_result = revalidate_exit_for_submission(
            approval,
            now=now,
            session_date=now.astimezone(ZoneInfo("America/New_York")).date(),
            position_asset_id=final_position.asset_id,
            position_symbol=final_position.symbol,
            position_side=final_position.side,
            current_quantity=final_position.quantity,
            order_side=order_side,
            limit_price=final_limit_price,
            quote_age_seconds=final_quote_age_seconds,
        )
        if final_result.decision is not RevalidationDecision.READY_TO_SUBMIT:
            await _finish(
                record.approval_id,
                state=(
                    ApprovalState.EXPIRED
                    if final_result.decision is RevalidationDecision.EXPIRED
                    else ApprovalState.CONDITION_FAILED
                ),
                now=now,
                reason=final_result.reason,
            )
            return
        if (
            final_snapshot.account.status.upper() != "ACTIVE"
            or final_snapshot.account.trading_blocked
            or final_snapshot.account.account_blocked
            or final_snapshot.account.trade_suspended_by_user
        ):
            await _finish(
                record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=datetime.now(UTC),
                reason="broker_account_unavailable",
            )
            return
        final_guardian_allowed, final_guardian_reason = await GuardianExecutionGate(
            PostgresGuardianStore(database.sessions, workspace_id)
        ).execution_allowed()
        if not final_guardian_allowed:
            await _finish(
                record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=datetime.now(UTC),
                reason=f"guardian_blocked:{final_guardian_reason}",
            )
            return
        submission_token = (
            None
            if claim_token is None
            else await store.authorize_submission(
                record.approval_id,
                workspace_id=workspace_id,
                now=now,
                claim_token=claim_token,
            )
        )
        if submission_token is None:
            return
        try:
            market_clock = await AlpacaMarketClockAdapter(
                str(secret["api_key_id"]),
                str(secret["secret_key"]),
                environment=environment,
            ).get_clock()
        except Exception as error:
            raise PreSubmissionCheckFailed("authoritative_market_clock_unavailable") from error
        if not market_clock.is_open:
            raise PreSubmissionCheckFailed("market_session_closed")
        if not await store.authorize_final_submission(
            record.approval_id,
            workspace_id=workspace_id,
            submission_token=submission_token,
        ):
            raise PreSubmissionCheckFailed("approval_ownership_changed")
        try:
            submission_started[0] = True
            order = await broker.submit_close_limit(
                symbol=record.position_symbol,
                quantity=record.max_quantity,
                limit_price=str(final_limit_price),
                order_side=order_side,
                client_order_id=record.client_order_id,
            )
        except BrokerPreflightFailed as error:
            raise PreSubmissionCheckFailed(str(error)) from error
        except Exception as error:
            raise SubmissionAttempted("close_submission_uncertain") from error
        if not _close_order_matches(
            order,
            client_order_id=record.client_order_id,
            symbol=record.position_symbol,
            order_side=order_side,
            quantity=record.max_quantity,
            expected_limit_price=final_limit_price,
            expected_broker_account_id=record.approved_broker_account_id,
            expected_environment=environment,
        ):
            await _finish(
                record.approval_id,
                state=ApprovalState.SUBMISSION_UNCERTAIN,
                now=datetime.now(UTC),
                reason="submitted_order_identity_mismatch",
                broker_order_id=order.broker_order_id or None,
                broker_order=order,
            )
            return
        if not _close_fill_response_is_valid(order, record.max_quantity):
            await _finish(
                record.approval_id,
                state=ApprovalState.SUBMISSION_UNCERTAIN,
                now=datetime.now(UTC),
                reason="broker_fill_response_invalid",
                broker_order_id=order.broker_order_id or None,
                broker_order=order,
            )
            return
        status = order.status.lower()
        final_state = _broker_order_state(
            status, order.filled_quantity, Decimal(record.max_quantity)
        )
        await _finish(
            record.approval_id,
            state=final_state,
            now=now,
            submitted_at=now,
            reason=order.status if final_state is ApprovalState.BROKER_REJECTED else None,
            broker_order_id=order.broker_order_id,
            broker_order=order,
        )
    finally:
        try:
            await broker.close()
        except Exception as error:
            if submission_started[0]:
                raise SubmissionAttempted("close_cleanup_uncertain") from error
            raise PreSubmissionCheckFailed("close_cleanup_failed") from error


async def _recover_ready_to_submit(
    database: Database,
    store: ConditionalApprovalStore,
    cipher: CredentialCipher,
    workspace_id: UUID,
) -> None:
    records = await store.list_ready_to_submit(workspace_id=workspace_id, approval_kind="CLOSE")
    records += await store.list_stale_submitting(
        workspace_id=workspace_id, approval_kind="CLOSE", now=datetime.now(UTC)
    )
    records += await store.list_dispatch_authorized(
        workspace_id=workspace_id, approval_kind="CLOSE"
    )
    if not records:
        return
    async with database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, workspace_id)
    if workspace is None:
        return
    environment = TradingEnvironment(workspace.trading_environment)
    provider = "ALPACA_LIVE" if environment is TradingEnvironment.LIVE else "ALPACA_PAPER"
    secret = await CredentialStore(database.sessions, cipher).reveal(workspace_id, provider)
    if secret is None:
        return
    broker = AlpacaBrokerAdapter(
        str(secret["api_key_id"]), str(secret["secret_key"]), environment=environment
    )
    try:
        for record in records:
            now = datetime.now(UTC)
            claim_token = record.claim_token
            if claim_token is None:
                await store.mark_unclaimable_recovery(
                    workspace_id=workspace_id,
                    approval_id=record.approval_id,
                    now=now,
                )
                continue
            bound_claim_token = claim_token

            async def _finish(
                approval_id: UUID,
                _claim_token: UUID = bound_claim_token,
                **kwargs: Any,
            ) -> None:
                await store.finish(
                    approval_id,
                    workspace_id=workspace_id,
                    claim_token=_claim_token,
                    **kwargs,
                )

            try:
                order = await broker.get_order(client_order_id=record.client_order_id)
            except Exception:
                continue
            if order is None:
                await _finish(
                    record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=now,
                    reason="ready_submission_recovery_no_broker_order",
                )
                continue
            try:
                recovered_side = ExitOrderSide(record.exit_order_side or "")
            except ValueError:
                await _finish(
                    record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=now,
                    reason="recovered_order_exit_side_invalid",
                )
                continue
            if not _close_fill_response_is_valid(order, record.max_quantity):
                await _finish(
                    record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=now,
                    reason="recovered_broker_fill_response_invalid",
                    broker_order_id=order.broker_order_id or None,
                    broker_order=order,
                )
                continue
            status = order.status.lower()
            final_state = (
                _broker_order_state(status, order.filled_quantity, Decimal(record.max_quantity))
                if _close_order_matches(
                    order,
                    client_order_id=record.client_order_id,
                    symbol=record.position_symbol or "",
                    order_side=recovered_side,
                    quantity=record.max_quantity,
                    limit_price_bound=(
                        record.min_limit_price
                        if recovered_side is ExitOrderSide.SELL
                        else record.max_limit_price
                    ),
                    expected_broker_account_id=record.approved_broker_account_id,
                    expected_environment=environment,
                )
                else ApprovalState.SUBMISSION_UNCERTAIN
            )
            if final_state is ApprovalState.SUBMISSION_UNCERTAIN:
                await _finish(
                    record.approval_id,
                    state=final_state,
                    now=now,
                    reason="recovered_order_identity_mismatch",
                    broker_order_id=order.broker_order_id or None,
                    broker_order=order,
                )
            else:
                await _finish(
                    record.approval_id,
                    state=final_state,
                    now=now,
                    submitted_at=order.submitted_at or now,
                    reason=(order.status if final_state is ApprovalState.BROKER_REJECTED else None),
                    broker_order_id=order.broker_order_id,
                    broker_order=order,
                )
    finally:
        await broker.close()
