from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from packages.execution.conditional_approval import ApprovalState
from packages.execution.conditional_store import ConditionalApprovalStore

NOW = datetime(2026, 9, 23, 1, tzinfo=UTC)
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000002")
CLAIM_TOKEN = UUID("00000000-0000-0000-0000-000000000003")
SUBMISSION_TOKEN = UUID("00000000-0000-0000-0000-000000000004")


def _database_for(record: SimpleNamespace) -> tuple[MagicMock, AsyncMock]:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=record)
    database = MagicMock()
    database.sessions.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    return database, session


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ApprovalState.REVALIDATING, ApprovalState.SUBMITTING])
async def test_release_pre_submission_returns_claim_to_approved(state: ApprovalState) -> None:
    record = SimpleNamespace(
        approval_id=APPROVAL_ID,
        workspace_id=WORKSPACE_ID,
        state=state,
        claimed_at=NOW,
        claim_token=CLAIM_TOKEN,
        submission_claimed_at=NOW,
        submission_token=SUBMISSION_TOKEN,
        failure_reason=None,
        updated_at=None,
    )
    database, session = _database_for(record)

    released = await ConditionalApprovalStore(database).release_pre_submission(
        APPROVAL_ID,
        workspace_id=WORKSPACE_ID,
        claim_token=CLAIM_TOKEN,
        now=NOW,
        reason="market_session_closed",
    )

    assert record.state is ApprovalState.APPROVED_FOR_SESSION
    assert record.claimed_at is None
    assert record.claim_token is None
    assert record.submission_claimed_at is None
    assert record.submission_token is None
    assert record.failure_reason == "market_session_closed"
    session.add.assert_called_once()
    assert released is True


@pytest.mark.asyncio
async def test_release_pre_submission_refuses_dispatch_authorized() -> None:
    record = SimpleNamespace(
        approval_id=APPROVAL_ID,
        workspace_id=WORKSPACE_ID,
        state=ApprovalState.DISPATCH_AUTHORIZED,
        claimed_at=NOW,
        claim_token=CLAIM_TOKEN,
        submission_claimed_at=NOW,
        submission_token=SUBMISSION_TOKEN,
        failure_reason=None,
        updated_at=None,
    )
    database, session = _database_for(record)

    released = await ConditionalApprovalStore(database).release_pre_submission(
        APPROVAL_ID,
        workspace_id=WORKSPACE_ID,
        claim_token=CLAIM_TOKEN,
        now=NOW,
        reason="market_session_closed",
    )

    assert record.state is ApprovalState.DISPATCH_AUTHORIZED
    assert record.claim_token is CLAIM_TOKEN
    assert record.submission_token is SUBMISSION_TOKEN
    session.add.assert_not_called()
    assert released is False
