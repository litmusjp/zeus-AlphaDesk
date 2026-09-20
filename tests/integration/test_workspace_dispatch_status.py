from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update

from packages.database.models import (
    AppUserRecord,
    AuditRecord,
    ConditionalApprovalRecord,
    WorkspaceRecord,
)
from packages.database.session import Database
from packages.domain.broker import BrokerOrder, OrderSubmission
from packages.domain.workflow import OrderIntent
from packages.execution.conditional_approval import ApprovalState
from packages.execution.conditional_store import ConditionalApprovalStore
from packages.execution.connected_paper import ApprovalSubmissionFence
from packages.execution.engine import ExecutionBlocked, ExecutionEngine, InMemoryIntentStore
from packages.replay.catalyst import run_catalyst_replay

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ALPHADESK_RUN_INTEGRATION") != "1",
        reason="Set ALPHADESK_RUN_INTEGRATION=1 to use local PostgreSQL.",
    ),
]

USER_ID = UUID("96000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("96000000-0000-0000-0000-000000000002")


class CountingAdapter:
    def __init__(self) -> None:
        self.submit_calls = 0

    async def submit_order(self, _submission: OrderSubmission) -> BrokerOrder:
        self.submit_calls += 1
        raise AssertionError("suspended workspace reached the broker adapter")


def _database() -> Database:
    return Database(
        os.getenv(
            "ALPHADESK_TEST_DATABASE_URL",
            "postgresql+psycopg://alphadesk:alphadesk_dev@localhost:5432/alphadesk",
        )
    )


async def _seed_claimed_approval(database: Database, *, approval_kind: str) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    approval_id = uuid4()
    submission_token = uuid4()
    async with database.sessions.begin() as session:
        session.add(
            AppUserRecord(
                user_id=USER_ID,
                auth_subject="dispatch-status-user",
                email="dispatch-status@example.test",
                is_admin=False,
                created_at=now,
                last_seen_at=now,
            )
        )
        session.add(
            WorkspaceRecord(
                workspace_id=WORKSPACE_ID,
                owner_user_id=USER_ID,
                name="Dispatch status integration",
                workspace_type="CONNECTED_PAPER",
                status="ACTIVE",
                scanner_enabled=False,
                assessment_policy={},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ConditionalApprovalRecord(
                approval_id=approval_id,
                workspace_id=WORKSPACE_ID,
                opportunity_id=None,
                approved_by_user_id=USER_ID,
                state=ApprovalState.SUBMITTING,
                approval_kind=approval_kind,
                session_date=now.date(),
                approved_at=now,
                expires_at=now,
                client_order_id=f"dispatch-status-{approval_kind.lower()}",
                structure_fingerprint="test",
                approved_intent_payload=None,
                approved_structure_identity=None,
                approved_broker_account_id=None,
                max_limit_price=1,
                max_loss=1,
                max_quantity=1,
                max_quote_age_seconds=30,
                min_limit_price=None,
                position_asset_id=None,
                position_symbol=None,
                position_side=None,
                exit_order_side=None,
                broker_order_id=None,
                claimed_at=now,
                claim_token=uuid4(),
                submission_claimed_at=now,
                submission_token=submission_token,
                dispatch_authorized_at=None,
                submitted_at=None,
                failure_reason=None,
                created_at=now,
                updated_at=now,
            )
        )
    return approval_id, submission_token


async def _suspend_and_cleanup(database: Database, approval_id: UUID) -> None:
    async with database.sessions.begin() as session:
        await session.execute(
            update(WorkspaceRecord)
            .where(WorkspaceRecord.workspace_id == WORKSPACE_ID)
            .values(status="SUSPENDED")
        )
        await session.execute(delete(AuditRecord).where(AuditRecord.workspace_id == WORKSPACE_ID))
        await session.execute(
            delete(ConditionalApprovalRecord).where(
                ConditionalApprovalRecord.approval_id == approval_id
            )
        )
        await session.execute(
            delete(WorkspaceRecord).where(WorkspaceRecord.workspace_id == WORKSPACE_ID)
        )
        await session.execute(delete(AppUserRecord).where(AppUserRecord.user_id == USER_ID))


@pytest.mark.asyncio
async def test_suspended_after_claim_cannot_authorize_open_or_submit() -> None:
    database = _database()
    approval_id, submission_token = await _seed_claimed_approval(database, approval_kind="OPEN")
    adapter = CountingAdapter()
    try:
        async with database.sessions.begin() as session:
            await session.execute(
                update(WorkspaceRecord)
                .where(WorkspaceRecord.workspace_id == WORKSPACE_ID)
                .values(status="SUSPENDED")
            )
        replay = run_catalyst_replay("approved")
        assert replay.order_intent is not None
        intent = OrderIntent.model_validate(replay.order_intent)
        engine = ExecutionEngine(
            adapter,
            InMemoryIntentStore(),
            submission_fence=ApprovalSubmissionFence(
                database=database,
                workspace_id=WORKSPACE_ID,
                approval_id=approval_id,
                submission_token=submission_token,
            ),
        )

        with pytest.raises(ExecutionBlocked, match="ownership changed"):
            await engine.execute(intent)
        assert adapter.submit_calls == 0
    finally:
        await _suspend_and_cleanup(database, approval_id)
        await database.close()


@pytest.mark.asyncio
async def test_suspended_after_claim_cannot_authorize_close_or_submit() -> None:
    database = _database()
    approval_id, submission_token = await _seed_claimed_approval(database, approval_kind="CLOSE")
    adapter = CountingAdapter()
    try:
        async with database.sessions.begin() as session:
            await session.execute(
                update(WorkspaceRecord)
                .where(WorkspaceRecord.workspace_id == WORKSPACE_ID)
                .values(status="SUSPENDED")
            )
        authorized = await ConditionalApprovalStore(database).authorize_final_submission(
            approval_id,
            workspace_id=WORKSPACE_ID,
            submission_token=submission_token,
        )
        if authorized:
            await adapter.submit_order(OrderSubmission.model_construct())
        assert not authorized
        assert adapter.submit_calls == 0
    finally:
        await _suspend_and_cleanup(database, approval_id)
        await database.close()
