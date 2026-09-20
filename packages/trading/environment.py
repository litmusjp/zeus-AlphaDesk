from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from packages.domain.system import TradingEnvironment

LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"


class ModeTransitionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_user_id: UUID
    is_admin: bool
    old_environment: TradingEnvironment
    new_environment: TradingEnvironment
    confirmation: str | None = None


class TradingModeReadiness(BaseModel):
    """Evidence evaluated immediately before a persisted mode transition."""

    model_config = ConfigDict(frozen=True)

    is_admin: bool
    target_credentials_verified: bool
    target_account_authenticated_at: datetime | None
    target_account_id: str | None
    reconciled_at: datetime | None
    stream_connected: bool
    account_active: bool
    account_unblocked: bool
    guardian_halted: bool
    active_or_unresolved_orders: int
    active_or_unresolved_approvals: int
    ambiguous_submissions: int
    active_submission_leases: int
    broker_confirmed_positions: int
    target_account_matches: bool
    legacy_identity: bool
    preparation_state: str
    prepared_at: datetime | None
    prepared_environment: str | None
    prepared_credential_fingerprint: str | None
    current_credential_fingerprint: str | None


class ModeTransitionDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str


def validate_mode_transition(
    request: ModeTransitionRequest,
    readiness: TradingModeReadiness,
    *,
    now: datetime | None = None,
    maximum_reconciliation_age: timedelta = timedelta(seconds=90),
    maximum_authentication_age: timedelta = timedelta(minutes=5),
) -> ModeTransitionDecision:
    """Validate the complete fail-closed mode-switch contract without I/O."""

    if not readiness.is_admin or not request.is_admin:
        return ModeTransitionDecision(allowed=False, reason="admin_required")
    if request.old_environment == request.new_environment:
        return ModeTransitionDecision(allowed=False, reason="environment_unchanged")
    evaluated_at = now or datetime.now(UTC)
    if request.new_environment is TradingEnvironment.LIVE:
        if request.confirmation != LIVE_CONFIRMATION_PHRASE:
            return ModeTransitionDecision(allowed=False, reason="live_confirmation_required")
        if readiness.preparation_state != "PREPARED":
            return ModeTransitionDecision(allowed=False, reason="live_preparation_missing")
        if readiness.prepared_environment != TradingEnvironment.LIVE.value:
            return ModeTransitionDecision(
                allowed=False, reason="live_preparation_environment_mismatch"
            )
        if not readiness.prepared_at:
            return ModeTransitionDecision(allowed=False, reason="live_preparation_stale")
        prepared_at = readiness.prepared_at
        if (
            prepared_at is not None
            and evaluated_at - prepared_at > maximum_reconciliation_age
        ):
            return ModeTransitionDecision(allowed=False, reason="live_preparation_stale")
        if (
            readiness.prepared_credential_fingerprint != readiness.current_credential_fingerprint
        ):
            return ModeTransitionDecision(
                allowed=False, reason="live_preparation_credentials_changed"
            )
    if not readiness.target_credentials_verified:
        return ModeTransitionDecision(allowed=False, reason="target_credentials_not_verified")
    if not readiness.target_account_id:
        return ModeTransitionDecision(allowed=False, reason="target_account_identity_missing")
    if readiness.target_account_authenticated_at is None:
        return ModeTransitionDecision(allowed=False, reason="target_account_not_authenticated")
    if evaluated_at - readiness.target_account_authenticated_at > maximum_authentication_age:
        return ModeTransitionDecision(allowed=False, reason="target_account_authentication_stale")
    if readiness.reconciled_at is None:
        return ModeTransitionDecision(allowed=False, reason="reconciliation_missing")
    if evaluated_at - readiness.reconciled_at > maximum_reconciliation_age:
        return ModeTransitionDecision(allowed=False, reason="reconciliation_stale")
    if not readiness.stream_connected and readiness.preparation_state != "PREPARED":
        return ModeTransitionDecision(allowed=False, reason="trade_updates_disconnected")
    if not readiness.account_active:
        return ModeTransitionDecision(allowed=False, reason="account_inactive")
    if not readiness.account_unblocked:
        return ModeTransitionDecision(allowed=False, reason="account_blocked")
    if readiness.guardian_halted:
        return ModeTransitionDecision(allowed=False, reason="guardian_halted")
    if readiness.legacy_identity:
        return ModeTransitionDecision(allowed=False, reason="legacy_identity")
    if not readiness.target_account_matches:
        return ModeTransitionDecision(allowed=False, reason="target_account_mismatch")
    checks = (
        (readiness.active_or_unresolved_orders, "unresolved_orders"),
        (readiness.active_or_unresolved_approvals, "unresolved_approvals"),
        (readiness.ambiguous_submissions, "ambiguous_submissions"),
        (readiness.active_submission_leases, "active_submission_leases"),
        (readiness.broker_confirmed_positions, "broker_positions_present"),
    )
    for count, reason in checks:
        if count:
            return ModeTransitionDecision(allowed=False, reason=reason)
    return ModeTransitionDecision(allowed=True, reason="ready")
