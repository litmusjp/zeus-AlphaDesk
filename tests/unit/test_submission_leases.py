from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from packages.domain.broker import OrderStatus
from packages.execution.conditional_approval import ApprovalState, approval_can_be_renewed
from packages.execution.conditional_store import (
    _dispatch_authorization_is_current,
    _submission_lease_is_stale,
)


def test_submission_recovery_uses_submission_lease_timestamp() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    cutoff = now - timedelta(minutes=10)
    record = SimpleNamespace(
        claimed_at=now - timedelta(hours=1),
        submission_claimed_at=now - timedelta(minutes=2),
    )

    assert _submission_lease_is_stale(record, cutoff) is False


def test_missing_submission_lease_timestamp_is_recovery_unsafe() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    cutoff = now - timedelta(minutes=10)
    record = SimpleNamespace(claimed_at=now, submission_claimed_at=None)

    assert _submission_lease_is_stale(record, cutoff) is True


def test_done_for_day_is_a_provider_order_status() -> None:
    assert OrderStatus.DONE_FOR_DAY.value == "done_for_day"


def test_dispatch_boundary_is_not_reclaimable_or_renewable() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    cutoff = now - timedelta(minutes=10)
    record = SimpleNamespace(
        state=ApprovalState.DISPATCH_AUTHORIZED,
        submission_claimed_at=now - timedelta(hours=1),
    )

    assert not _submission_lease_is_stale(record, cutoff)
    assert not approval_can_be_renewed(ApprovalState.DISPATCH_AUTHORIZED)


def test_dispatch_authorization_rejects_expired_submission() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert not _dispatch_authorization_is_current(
        state=ApprovalState.SUBMITTING,
        expires_at=now - timedelta(microseconds=1),
        now=now,
    )
