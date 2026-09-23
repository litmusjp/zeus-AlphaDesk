from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import UUID

import pytest

import packages.execution.conditional_exit_runner as exit_runner
from packages.execution.conditional_approval import ApprovalState

NOW = datetime(2026, 9, 23, 1, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")


def _claimed_close_record() -> SimpleNamespace:
    return SimpleNamespace(
        approval_id=UUID("00000000-0000-0000-0000-000000000002"),
        claim_token=UUID("00000000-0000-0000-0000-000000000003"),
    )


async def _run_with_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    *,
    release_result: bool = True,
    initial_state: ApprovalState = ApprovalState.REVALIDATING,
) -> tuple[AsyncMock, AsyncMock, SimpleNamespace]:
    record = _claimed_close_record()
    record.state = initial_state
    store = AsyncMock()
    store.claim_next.side_effect = [record, None]

    async def release_pre_submission(*_args: object, **_kwargs: object) -> bool:
        if release_result:
            record.state = ApprovalState.APPROVED_FOR_SESSION
        return release_result

    store.release_pre_submission.side_effect = release_pre_submission
    monkeypatch.setattr(exit_runner, "ConditionalApprovalStore", lambda _database: store)
    monkeypatch.setattr(exit_runner, "_recover_ready_to_submit", AsyncMock())
    monkeypatch.setattr(
        exit_runner,
        "_process_claimed_exit",
        AsyncMock(side_effect=failure),
    )

    session = AsyncMock()
    session.get.return_value = SimpleNamespace(trading_environment="PAPER")
    database = MagicMock()
    database.sessions.return_value.__aenter__.return_value = session
    credentials = MagicMock()
    credentials.return_value.reveal = AsyncMock(
        return_value={"api_key_id": "test-key", "secret_key": "test-secret"}
    )
    monkeypatch.setattr(exit_runner, "CredentialStore", credentials)

    await exit_runner.process_workspace_exit_approvals(
        database=database,
        cipher=MagicMock(),
        workspace_id=WORKSPACE_ID,
        now=NOW,
    )
    return store, store.finish, record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "market_session_closed",
        "authoritative_market_clock_unavailable",
        "initial_broker_check_failed",
        "option_quote_check_failed",
        "final_broker_check_failed",
        "final_option_quote_check_failed",
        "close_cleanup_failed",
        "approval_ownership_changed",
        "Alpaca market session is closed",
    ],
)
async def test_transient_exit_pre_submission_failure_releases_for_retry(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    store, finish, record = await _run_with_failure(
        monkeypatch, exit_runner.PreSubmissionCheckFailed(reason)
    )

    store.release_pre_submission.assert_awaited_once_with(
        record.approval_id,
        workspace_id=WORKSPACE_ID,
        claim_token=record.claim_token,
        now=ANY,
        reason=reason,
    )
    finish.assert_not_awaited()
    assert record.state is ApprovalState.APPROVED_FOR_SESSION


@pytest.mark.asyncio
async def test_transient_exit_failure_after_dispatch_authorization_needs_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, finish, record = await _run_with_failure(
        monkeypatch,
        exit_runner.PreSubmissionCheckFailed("market_session_closed"),
        release_result=False,
        initial_state=ApprovalState.DISPATCH_AUTHORIZED,
    )

    store.release_pre_submission.assert_awaited_once()
    finish.assert_awaited_once()
    await_args = finish.await_args
    assert await_args is not None
    assert await_args.kwargs["state"] is ApprovalState.SUBMISSION_UNCERTAIN
    assert await_args.kwargs["reason"] == "dispatch_authorized_requires_reconciliation"
    assert record.state is ApprovalState.DISPATCH_AUTHORIZED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "invalid_exit_plan",
        "quote_stale",
        "close_position_or_order_changed",
        "guardian_blocked:manual_hold",
        "invalid_limit_price",
        "approval_expired",
    ],
)
async def test_deterministic_exit_pre_submission_failure_remains_terminal(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    store, finish, _ = await _run_with_failure(
        monkeypatch, exit_runner.PreSubmissionCheckFailed(reason)
    )

    store.release_pre_submission.assert_not_awaited()
    finish.assert_awaited_once()
    await_args = finish.await_args
    assert await_args is not None
    assert await_args.kwargs["state"] is ApprovalState.CONDITION_FAILED
    assert await_args.kwargs["reason"] == reason


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("initial_broker_check_failed", True),
        ("Alpaca market session is closed", True),
        ("unknown_check_failed", False),
        ("unknown_unavailable", False),
        ("broker preflight failed", False),
        ("market session is closed because of maintenance", False),
    ],
)
def test_pre_submission_retry_reasons_use_exact_allowlist(reason: str, expected: bool) -> None:
    assert exit_runner._is_retryable_pre_submission_failure(reason) is expected
