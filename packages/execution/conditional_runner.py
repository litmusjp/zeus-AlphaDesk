from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from typing import Any
from uuid import UUID

from sqlalchemy import select

from packages.broker.alpaca_adapter import AlpacaBrokerAdapter
from packages.broker.validation import validate_broker_order_leg_identity
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.market_clock import AlpacaMarketClockAdapter, ConnectedMarketClock
from packages.connected.opportunities import ConnectedAnalysis, ConnectedOpportunityService
from packages.connected.option_scan_policy import ScanMode
from packages.database.models import ConnectedOpportunityRecord, WorkspaceRecord
from packages.database.session import Database
from packages.domain.broker import BrokerOrder
from packages.domain.system import TradingEnvironment
from packages.domain.workflow import OrderIntent, RankedCandidate
from packages.execution.conditional_approval import (
    ApprovalState,
    ConditionalApproval,
    RevalidationDecision,
    candidate_structure_identity,
    order_structure_fingerprint,
    revalidate_for_submission,
    validate_exit_plan,
)
from packages.execution.conditional_store import ConditionalApprovalStore
from packages.execution.connected_paper import PreSubmissionCheckFailed, execute_connected_order
from packages.execution.engine import ExecutionBlocked, SubmissionUncertain
from packages.execution.order_state import BrokerFillState, broker_fill_state
from packages.security.credentials import CredentialCipher
from packages.security.store import CredentialStore


def _open_order_matches(
    order: BrokerOrder,
    intent: OrderIntent,
    *,
    approved_client_order_id: str,
    expected_broker_account_id: str | None = None,
) -> bool:
    return (
        bool(order.broker_order_id)
        and (
            expected_broker_account_id is None
            or (
                order.broker_account_id == expected_broker_account_id
                and order.environment == "PAPER"
            )
        )
        and order.client_order_id == approved_client_order_id
        and intent.client_order_id == approved_client_order_id
        and order.asset_class == "us_option"
        and order.quantity == intent.quantity
        and order.order_type.lower() == "limit"
        and order.order_class.lower() == "mleg"
        and order.time_in_force.lower() == "day"
        and order.limit_price == intent.limit_price
        and len(order.legs) == len(intent.legs)
        and all(
            broker_leg.symbol == intent_leg.symbol
            and broker_leg.side == intent_leg.side
            and broker_leg.quantity == intent_leg.ratio
            for broker_leg, intent_leg in zip(order.legs, intent.legs, strict=True)
        )
    )


def _candidate_matches_intent(candidate: RankedCandidate, intent: OrderIntent) -> bool:
    candidate_legs = tuple(
        (
            leg.contract.symbol,
            "buy" if leg.side.value == "long" else "sell",
            leg.ratio,
        )
        for leg in candidate.structure.legs
    )
    intent_legs = tuple((leg.symbol, leg.side, leg.ratio) for leg in intent.legs)
    return candidate.structure.quantity == intent.quantity and candidate_legs == intent_legs


def _candidate_structure_identity(candidate: RankedCandidate) -> dict[str, object]:
    return candidate_structure_identity(candidate)


def _intent_matches_approved(candidate: OrderIntent, approved: OrderIntent) -> bool:
    return candidate.model_dump(mode="json") == approved.model_dump(mode="json")


def _open_order_state(order: BrokerOrder) -> ApprovalState:
    if order.quantity is None:
        return ApprovalState.SUBMISSION_UNCERTAIN
    state = broker_fill_state(order.status, order.filled_quantity, order.quantity)
    if state is None:
        return ApprovalState.SUBMISSION_UNCERTAIN
    return {
        BrokerFillState.SUBMITTED: ApprovalState.SUBMITTED,
        BrokerFillState.PARTIALLY_FILLED: ApprovalState.PARTIALLY_FILLED,
        BrokerFillState.FILLED: ApprovalState.FILLED,
        BrokerFillState.BROKER_REJECTED: ApprovalState.BROKER_REJECTED,
    }.get(state, ApprovalState.SUBMISSION_UNCERTAIN)


def _open_fill_response_is_valid(order: BrokerOrder) -> bool:
    if validate_broker_order_leg_identity(order) is not None:
        return False
    if (
        order.quantity is None
        or not order.quantity.is_finite()
        or not order.filled_quantity.is_finite()
        or order.filled_quantity < 0
        or order.filled_quantity > order.quantity
    ):
        return False
    status = order.status.lower()
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
    if status in {"new", "accepted", "pending_new"} and order.filled_quantity != 0:
        return False
    if order.filled_average_price is not None and (
        not order.filled_average_price.is_finite() or order.filled_average_price <= 0
    ):
        return False
    if order.filled_quantity > 0 and (
        order.filled_average_price is None
        or not order.filled_average_price.is_finite()
        or order.filled_average_price <= 0
    ):
        return False
    if broker_fill_state(status, order.filled_quantity, order.quantity) is None:
        return False
    known_leg_statuses = {
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
    }
    for leg in order.legs:
        if leg.status.lower() not in known_leg_statuses:
            return False
        if not leg.filled_quantity.is_finite() or leg.filled_quantity < 0:
            return False
        if leg.quantity is not None and (
            not leg.quantity.is_finite() or leg.quantity <= 0 or leg.filled_quantity > leg.quantity
        ):
            return False
    if order.filled_quantity == 0 and any(leg.filled_quantity != 0 for leg in order.legs):
        return False
    if Decimal("0") < order.filled_quantity < order.quantity and any(
        leg.quantity is None
        or leg.filled_quantity != leg.quantity * order.filled_quantity / order.quantity
        for leg in order.legs
    ):
        return False
    if order.filled_quantity == order.quantity and any(
        leg.status.lower() not in {"filled", "canceled", "expired", "replaced"}
        or leg.quantity is None
        or leg.filled_quantity != leg.quantity
        for leg in order.legs
    ):
        return False
    if Decimal("0") < order.filled_quantity < order.quantity and any(
        leg.status.lower() == "filled" for leg in order.legs
    ):
        return False
    return True


def _opening_clock_allows_submission(clock: ConnectedMarketClock | None) -> bool:
    """Only the connected broker clock can authorize current-session opening."""
    return clock is not None and clock.is_open


def _opening_market_clock_failure_reason(clock: ConnectedMarketClock | None) -> str | None:
    if clock is None:
        return "authoritative_market_clock_unavailable"
    if not clock.is_open:
        return "market_session_closed"
    return None


async def process_workspace_approvals(
    *, database: Database, cipher: CredentialCipher, workspace_id: UUID, now: datetime
) -> None:
    store = ConditionalApprovalStore(database)
    now = datetime.now(UTC)
    await store.reclaim_stale_revalidating(workspace_id=workspace_id, now=now)
    await _recover_ready_to_submit(database, store, cipher, workspace_id)
    await store.expire_before(workspace_id=workspace_id, now=now)
    while True:
        now = datetime.now(UTC)
        approval_record = await store.claim_next(
            workspace_id=workspace_id, now=now
        )
        if approval_record is None:
            return
        claim_token = approval_record.claim_token
        if claim_token is None:
            await store.mark_unclaimable_recovery(
                workspace_id=workspace_id,
                approval_id=approval_record.approval_id,
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

        submission_started = False
        submission_token: UUID | None = None
        try:
            if approval_record.opportunity_id is None:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="opportunity_id_missing",
                )
                continue
            async with database.sessions() as session:
                original_record = await session.scalar(
                    select(ConnectedOpportunityRecord).where(
                        ConnectedOpportunityRecord.workspace_id == workspace_id,
                        ConnectedOpportunityRecord.opportunity_id == approval_record.opportunity_id,
                    )
                )
                workspace = await session.scalar(
                    select(WorkspaceRecord).where(
                        WorkspaceRecord.workspace_id == workspace_id
                    )
                )
            if original_record is None:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="opportunity_missing",
                )
                continue
            original = ConnectedAnalysis.model_validate(original_record.payload)
            if (
                approval_record.approved_intent_payload is None
                or approval_record.approved_structure_identity is None
            ):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="approved_snapshot_missing",
                )
                continue
            invalid_exit_plan = validate_exit_plan(approval_record.exit_plan_payload)
            if invalid_exit_plan:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason=invalid_exit_plan,
                )
                continue
            approved_intent = OrderIntent.model_validate(approval_record.approved_intent_payload)
            if workspace is None:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="workspace_missing",
                )
                continue
            environment = TradingEnvironment(workspace.trading_environment)
            provider = "ALPACA_LIVE" if environment is TradingEnvironment.LIVE else "ALPACA_PAPER"
            workspace_policy = workspace.assessment_policy
            secret = await CredentialStore(database.sessions, cipher).reveal(
                workspace_id, provider
            )
            if secret is None:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="alpaca_credential_unavailable",
                )
                continue
            service = ConnectedOpportunityService(
                database.sessions,
                workspace_id,
                str(secret["api_key_id"]),
                str(secret["secret_key"]),
                policy=AssessmentPolicy.from_payload(workspace_policy or {}),
                environment=environment,
            )
            fresh = await service.analyze(
                original.symbol,
                mode=ScanMode.EXECUTION,
                approved_intent=approved_intent,
            )
            if (
                fresh.disposition != "TRADE"
                or fresh.order_intent is None
                or fresh.candidate is None
            ):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason=f"fresh_disposition_{fresh.disposition.lower()}",
                )
                continue
            fresh_intent = OrderIntent.model_validate(fresh.order_intent)
            fresh_candidate = RankedCandidate.model_validate(fresh.candidate)
            if not _intent_matches_approved(fresh_intent, approved_intent):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=datetime.now(UTC),
                    reason="approved_intent_changed",
                )
                continue
            if (
                _candidate_structure_identity(fresh_candidate)
                != approval_record.approved_structure_identity
            ):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=datetime.now(UTC),
                    reason="approved_structure_changed",
                )
                continue
            if not _candidate_matches_intent(fresh_candidate, fresh_intent):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=datetime.now(UTC),
                    reason="fresh_candidate_intent_mismatch",
                )
                continue
            if fresh_intent.client_order_id != approval_record.client_order_id:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=datetime.now(UTC),
                    reason="approved_client_order_id_changed",
                )
                continue
            now = datetime.now(UTC)
            approval = ConditionalApproval(
                approval_id=approval_record.approval_id,
                workspace_id=approval_record.workspace_id,
                opportunity_id=approval_record.opportunity_id,
                client_order_id=approval_record.client_order_id,
                session_date=approval_record.session_date,
                approved_at=approval_record.approved_at,
                expires_at=approval_record.expires_at,
                structure_fingerprint=approval_record.structure_fingerprint,
                max_limit_price=approval_record.max_limit_price,
                max_loss=approval_record.max_loss,
                max_quantity=approval_record.max_quantity,
                max_quote_age_seconds=approval_record.max_quote_age_seconds,
                state=ApprovalState.APPROVED_FOR_SESSION,
                exit_plan=approval_record.exit_plan_payload,
            )
            if fresh.observed_at > now:
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="quote_timestamp_in_future",
                )
                continue
            leg_quote_times = tuple(
                leg.contract.quote.quoted_at for leg in fresh_candidate.structure.legs
            )
            if not leg_quote_times or any(quoted_at > now for quoted_at in leg_quote_times):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.CONDITION_FAILED,
                    now=now,
                    reason="option_quote_timestamp_invalid",
                )
                continue
            quote_age_seconds = max(0, ceil((now - min(leg_quote_times)).total_seconds()))
            result = revalidate_for_submission(
                approval,
                now=now,
                session_date=approval_record.session_date,
                structure_fingerprint=order_structure_fingerprint(fresh_intent),
                limit_price=fresh_intent.limit_price,
                maximum_loss=fresh_candidate.structure.max_loss,
                quantity=fresh_intent.quantity,
                quote_age_seconds=quote_age_seconds,
            )
            if result.decision is not RevalidationDecision.READY_TO_SUBMIT:
                await _finish(
                    approval_record.approval_id,
                    state=(
                        ApprovalState.EXPIRED
                        if result.decision is RevalidationDecision.EXPIRED
                        else ApprovalState.CONDITION_FAILED
                    ),
                    now=now,
                    reason=result.reason,
                )
                continue
            now = datetime.now(UTC)
            try:
                clock = await AlpacaMarketClockAdapter(
                    str(secret["api_key_id"]),
                    str(secret["secret_key"]),
                    environment=environment,
                ).get_clock()
            except Exception:
                clock = None
            clock_failure_reason = _opening_market_clock_failure_reason(clock)
            if clock_failure_reason is not None:
                await store.release_revalidation(
                    approval_record.approval_id,
                    workspace_id=workspace_id,
                    claim_token=bound_claim_token,
                    now=datetime.now(UTC),
                    reason=clock_failure_reason,
                )
                return
            submission_token = await store.authorize_submission(
                approval_record.approval_id,
                workspace_id=workspace_id,
                now=now,
                claim_token=claim_token,
            )
            if submission_token is None:
                continue
            submission_started = True
            broker_order = await execute_connected_order(
                database=database,
                cipher=cipher,
                workspace_id=workspace_id,
                candidate=fresh_candidate,
                intent=fresh_intent,
                approved_broker_account_id=approval_record.approved_broker_account_id,
                policy=service.policy,
                approval_id=approval_record.approval_id,
                claim_token=claim_token,
                submission_token=submission_token,
                environment=environment,
            )
            if not _open_order_matches(
                broker_order,
                fresh_intent,
                approved_client_order_id=approval_record.client_order_id,
                expected_broker_account_id=approval_record.approved_broker_account_id,
            ):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=datetime.now(UTC),
                    reason="submitted_order_identity_mismatch",
                    broker_order_id=broker_order.broker_order_id or None,
                    broker_order=broker_order,
                )
                continue
            if not _open_fill_response_is_valid(broker_order):
                await _finish(
                    approval_record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=datetime.now(UTC),
                    reason="broker_fill_response_invalid",
                    broker_order_id=broker_order.broker_order_id or None,
                    broker_order=broker_order,
                )
                continue
            final_state = _open_order_state(broker_order)
            await _finish(
                approval_record.approval_id,
                state=final_state,
                now=now,
                submitted_at=now,
                reason=(
                    broker_order.status if final_state is ApprovalState.BROKER_REJECTED else None
                ),
                broker_order_id=broker_order.broker_order_id,
                broker_order=broker_order,
            )
        except PreSubmissionCheckFailed as error:
            if submission_token is not None and str(error) in {
                "authoritative_market_clock_unavailable",
                "market_session_closed",
            }:
                await store.release_submission(
                    approval_record.approval_id,
                    workspace_id=workspace_id,
                    claim_token=bound_claim_token,
                    submission_token=submission_token,
                    now=datetime.now(UTC),
                    reason=str(error),
                )
                return
            await _finish(
                approval_record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=datetime.now(UTC),
                reason=str(error),
            )
        except SubmissionUncertain as error:
            await _finish(
                approval_record.approval_id,
                state=ApprovalState.SUBMISSION_UNCERTAIN,
                now=now,
                reason=str(error),
            )
        except ExecutionBlocked as error:
            await _finish(
                approval_record.approval_id,
                state=ApprovalState.CONDITION_FAILED,
                now=now,
                reason=type(error).__name__,
            )
        except Exception as error:
            await _finish(
                approval_record.approval_id,
                state=(
                    ApprovalState.SUBMISSION_UNCERTAIN
                    if submission_started
                    else ApprovalState.CONDITION_FAILED
                ),
                now=now,
                reason=type(error).__name__,
            )


async def _recover_ready_to_submit(
    database: Database,
    store: ConditionalApprovalStore,
    cipher: CredentialCipher,
    workspace_id: UUID,
) -> None:
    records = await store.list_ready_to_submit(workspace_id=workspace_id, approval_kind="OPEN")
    records += await store.list_stale_submitting(
        workspace_id=workspace_id, approval_kind="OPEN", now=datetime.now(UTC)
    )
    records += await store.list_dispatch_authorized(workspace_id=workspace_id, approval_kind="OPEN")
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
            if not _open_fill_response_is_valid(order):
                await _finish(
                    record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=now,
                    reason="recovered_broker_fill_response_invalid",
                    broker_order_id=order.broker_order_id or None,
                    broker_order=order,
                )
                continue
            if record.approved_intent_payload is None:
                await _finish(
                    record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=now,
                    reason="recovered_approved_snapshot_missing",
                )
                continue
            recovered_intent = OrderIntent.model_validate(record.approved_intent_payload)
            if not _open_order_matches(
                order,
                recovered_intent,
                approved_client_order_id=record.client_order_id,
                expected_broker_account_id=record.approved_broker_account_id,
            ):
                await _finish(
                    record.approval_id,
                    state=ApprovalState.SUBMISSION_UNCERTAIN,
                    now=now,
                    reason="recovered_order_identity_mismatch",
                    broker_order_id=order.broker_order_id or None,
                    broker_order=order,
                )
                continue
            final_state = _open_order_state(order)
            await _finish(
                record.approval_id,
                state=final_state,
                now=now,
                submitted_at=order.submitted_at or now,
                reason=order.status if final_state is ApprovalState.BROKER_REJECTED else None,
                broker_order_id=order.broker_order_id,
                broker_order=order,
            )
    finally:
        await broker.close()
