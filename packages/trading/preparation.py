from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from packages.domain.broker import ReconciliationSnapshot
from packages.domain.system import TradingEnvironment


class TargetBroker(Protocol):
    async def reconcile(self) -> ReconciliationSnapshot: ...
    async def close(self) -> None: ...


class LivePreparationError(RuntimeError):
    pass


async def prepare_live_target(
    broker: TargetBroker,
    *,
    credential_fingerprint: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Authenticate and reconcile LIVE without submitting or creating projections."""
    snapshot = await broker.reconcile()
    if snapshot.account.environment != TradingEnvironment.LIVE.value:
        raise LivePreparationError("broker_environment_mismatch")
    if not snapshot.account.account_id:
        raise LivePreparationError("target_account_identity_missing")
    if snapshot.account.status.upper() != "ACTIVE":
        raise LivePreparationError("account_inactive")
    if (
        snapshot.account.trading_blocked
        or snapshot.account.account_blocked
        or snapshot.account.trade_suspended_by_user
    ):
        raise LivePreparationError("account_blocked")
    prepared_at = now or datetime.now(UTC)
    return {
        "state": "PREPARED",
        "target_environment": TradingEnvironment.LIVE.value,
        "target_account_id": snapshot.account.account_id,
        "credential_fingerprint": credential_fingerprint,
        "authenticated_at": prepared_at,
        "reconciled_at": snapshot.reconciled_at,
        "prepared_at": prepared_at,
        "account_active": True,
        "account_unblocked": True,
        # A one-shot REST reconciliation is not a trade-updates stream probe.
        "stream_connected": False,
        "failure_reason": None,
    }
