from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from packages.domain.workflow import OrderIntent, RankedCandidate


class ApprovalState(StrEnum):
    APPROVED_FOR_SESSION = "APPROVED_FOR_SESSION"
    REVALIDATING = "REVALIDATING"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTING = "SUBMITTING"
    DISPATCH_AUTHORIZED = "DISPATCH_AUTHORIZED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    BROKER_REJECTED = "BROKER_REJECTED"
    CONDITION_FAILED = "CONDITION_FAILED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class ExitOrderSide(StrEnum):
    SELL = "sell"
    BUY = "buy"


class RevalidationDecision(StrEnum):
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    CONDITION_FAILED = "CONDITION_FAILED"
    EXPIRED = "EXPIRED"


def approval_is_active(state: ApprovalState | str, expires_at: datetime, now: datetime) -> bool:
    normalized_state = ApprovalState(state)
    if normalized_state is ApprovalState.REVALIDATING:
        return True
    return normalized_state is ApprovalState.APPROVED_FOR_SESSION and expires_at > now


def approval_can_be_renewed(state: ApprovalState | str) -> bool:
    return ApprovalState(state) in {
        ApprovalState.EXPIRED,
        ApprovalState.CONDITION_FAILED,
        ApprovalState.REJECTED,
    }


@dataclass(frozen=True)
class ConditionalApproval:
    approval_id: UUID
    workspace_id: UUID
    opportunity_id: UUID
    client_order_id: str
    session_date: date
    approved_at: datetime
    expires_at: datetime
    structure_fingerprint: str
    max_limit_price: Decimal
    max_loss: Decimal
    max_quantity: int
    max_quote_age_seconds: int
    state: ApprovalState


@dataclass(frozen=True)
class ConditionalExitApproval:
    approval_id: UUID
    workspace_id: UUID
    position_asset_id: str
    position_symbol: str
    position_side: str
    approved_quantity: Decimal
    order_side: ExitOrderSide
    session_date: date
    approved_at: datetime
    expires_at: datetime
    client_order_id: str
    limit_price_bound: Decimal
    max_quote_age_seconds: int
    state: ApprovalState


@dataclass(frozen=True)
class RevalidationResult:
    decision: RevalidationDecision
    reason: str | None = None


def candidate_structure_identity(candidate: RankedCandidate) -> dict[str, object]:
    structure = candidate.structure
    return {
        "structure_type": structure.structure_type.value,
        "quantity": structure.quantity,
        "underlying": structure.underlying.symbol if structure.underlying is not None else None,
        "legs": [
            {
                "symbol": leg.contract.symbol,
                "side": leg.side.value,
                "ratio": leg.ratio,
            }
            for leg in structure.legs
        ],
    }


def order_structure_fingerprint(intent: OrderIntent) -> str:
    return "|".join(f"{leg.symbol}:{leg.side}:{leg.ratio}" for leg in intent.legs)


def _invalid_decimal(value: Decimal) -> bool:
    return not value.is_finite() or value < 0


def revalidate_for_submission(
    approval: ConditionalApproval,
    *,
    now: datetime,
    session_date: date,
    structure_fingerprint: str,
    limit_price: Decimal,
    maximum_loss: Decimal,
    quantity: int,
    quote_age_seconds: int,
) -> RevalidationResult:
    if approval.state is not ApprovalState.APPROVED_FOR_SESSION:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "approval_not_pending")
    if session_date != approval.session_date or now >= approval.expires_at:
        return RevalidationResult(RevalidationDecision.EXPIRED, "approval_session_mismatch")
    if structure_fingerprint != approval.structure_fingerprint:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "structure_changed")
    if quantity < 1 or quantity > approval.max_quantity:
        return RevalidationResult(
            RevalidationDecision.CONDITION_FAILED, "quantity_exceeds_approval"
        )
    if quote_age_seconds < 0 or quote_age_seconds > approval.max_quote_age_seconds:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "quote_stale")
    if _invalid_decimal(limit_price) or limit_price <= 0 or limit_price > approval.max_limit_price:
        return RevalidationResult(
            RevalidationDecision.CONDITION_FAILED, "limit_price_exceeds_approval"
        )
    if _invalid_decimal(maximum_loss) or maximum_loss > approval.max_loss:
        return RevalidationResult(
            RevalidationDecision.CONDITION_FAILED, "maximum_loss_exceeds_approval"
        )
    return RevalidationResult(RevalidationDecision.READY_TO_SUBMIT)


def revalidate_exit_for_submission(
    approval: ConditionalExitApproval,
    *,
    now: datetime,
    session_date: date,
    position_asset_id: str,
    position_symbol: str,
    position_side: str,
    current_quantity: Decimal,
    order_side: ExitOrderSide,
    limit_price: Decimal,
    quote_age_seconds: int,
) -> RevalidationResult:
    if approval.state is not ApprovalState.APPROVED_FOR_SESSION:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "approval_not_pending")
    if session_date != approval.session_date or now >= approval.expires_at:
        return RevalidationResult(RevalidationDecision.EXPIRED, "approval_session_mismatch")
    if (
        position_asset_id != approval.position_asset_id
        or position_symbol != approval.position_symbol
        or position_side != approval.position_side
        or order_side is not approval.order_side
    ):
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "position_changed")
    if current_quantity != approval.approved_quantity or current_quantity <= 0:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "position_changed")
    if quote_age_seconds < 0 or quote_age_seconds > approval.max_quote_age_seconds:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "quote_stale")
    if _invalid_decimal(limit_price) or limit_price <= 0:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "limit_price_invalid")
    if approval.order_side is ExitOrderSide.SELL:
        if limit_price < approval.limit_price_bound:
            return RevalidationResult(
                RevalidationDecision.CONDITION_FAILED, "exit_limit_below_floor"
            )
    elif limit_price > approval.limit_price_bound:
        return RevalidationResult(RevalidationDecision.CONDITION_FAILED, "exit_limit_above_ceiling")
    return RevalidationResult(RevalidationDecision.READY_TO_SUBMIT)
