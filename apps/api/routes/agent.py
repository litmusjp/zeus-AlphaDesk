# FastAPI dependencies are intentionally declared in parameter defaults.
# ruff: noqa: B008
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from packages.auth.dependencies import WorkspaceContext, require_workspace
from packages.broker.projections import PostgresBrokerProjectionStore
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.strategy_assessment import (
    StrategyAssessmentRequest,
    StrategyAssessmentResult,
    assess_strategy,
)
from packages.database.models import AgentAPIKeyRecord, WorkspaceRecord
from packages.domain.broker import BrokerAccount, BrokerSyncStatus
from packages.domain.system import BrokerState
from packages.security.agent_keys import generate_agent_key, hash_agent_key, key_prefix

router = APIRouter(prefix="/desk", tags=["agent-integrations"])


def _broker_evidence_is_ready(
    status: BrokerSyncStatus,
    account: BrokerAccount | None,
    *,
    now: datetime,
    maximum_age: timedelta,
) -> bool:
    if (
        status.state is not BrokerState.RECONCILED
        or not status.stream_connected
        or status.last_reconciled_at is None
        or account is None
        or account.environment.upper() != "PAPER"
        or account.status.upper() != "ACTIVE"
        or account.trading_blocked
        or account.account_blocked
        or account.trade_suspended_by_user
    ):
        return False
    return all(
        timedelta(0) <= now - timestamp <= maximum_age
        for timestamp in (status.last_reconciled_at, account.as_of)
    )


class AgentKeyCreate(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str = Field(min_length=1, max_length=120)


class AgentKeyView(BaseModel):
    model_config = ConfigDict(frozen=True)
    key_id: UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class AgentKeyCreated(AgentKeyView):
    secret: str


@dataclass(frozen=True)
class AgentKeyContext:
    workspace_id: UUID
    key_id: UUID


async def require_agent_key(
    request: Request,
    api_key: Annotated[str | None, Header(alias="X-AlphaDesk-API-Key")] = None,
) -> AgentKeyContext:
    if not api_key:
        raise HTTPException(status_code=401, detail="X-AlphaDesk-API-Key is required")
    database = request.app.state.database
    if database is None:
        raise HTTPException(status_code=503, detail="Database infrastructure is unavailable")
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        record = await session.scalar(
            select(AgentAPIKeyRecord)
            .where(
                AgentAPIKeyRecord.key_hash == hash_agent_key(api_key),
                AgentAPIKeyRecord.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if record is None:
            raise HTTPException(status_code=401, detail="Unknown or revoked agent API key")
        workspace = await session.get(WorkspaceRecord, record.workspace_id)
        if workspace is None or workspace.status == "SUSPENDED":
            raise HTTPException(status_code=403, detail="Workspace is unavailable")
        record.last_used_at = now
        return AgentKeyContext(workspace_id=record.workspace_id, key_id=record.key_id)


def _view(record: AgentAPIKeyRecord) -> AgentKeyView:
    return AgentKeyView(
        key_id=record.key_id,
        name=record.name,
        prefix=record.key_prefix,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
    )


@router.post("/agent-api-keys", response_model=AgentKeyCreated)
async def create_agent_api_key(
    payload: AgentKeyCreate,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> AgentKeyCreated:
    secret = generate_agent_key()
    now = datetime.now(UTC)
    record = AgentAPIKeyRecord(
        key_id=uuid4(),
        workspace_id=context.workspace_id,
        key_hash=hash_agent_key(secret),
        key_prefix=key_prefix(secret),
        name=payload.name,
        created_at=now,
        last_used_at=None,
        revoked_at=None,
    )
    async with request.app.state.database.sessions.begin() as session:
        session.add(record)
    return AgentKeyCreated(**_view(record).model_dump(), secret=secret)


@router.get("/agent-api-keys", response_model=list[AgentKeyView])
async def list_agent_api_keys(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> list[AgentKeyView]:
    async with request.app.state.database.sessions() as session:
        records = await session.scalars(
            select(AgentAPIKeyRecord)
            .where(AgentAPIKeyRecord.workspace_id == context.workspace_id)
            .order_by(AgentAPIKeyRecord.created_at)
        )
        return [_view(record) for record in records]


@router.post("/agent-api-keys/{key_id}/revoke", response_model=AgentKeyView)
async def revoke_agent_api_key(
    key_id: UUID, request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> AgentKeyView:
    async with request.app.state.database.sessions.begin() as session:
        record = await session.scalar(
            select(AgentAPIKeyRecord)
            .where(
                AgentAPIKeyRecord.key_id == key_id,
                AgentAPIKeyRecord.workspace_id == context.workspace_id,
            )
            .with_for_update()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Agent API key not found")
        record.revoked_at = datetime.now(UTC)
        return _view(record)


@router.post("/agent-api-keys/{key_id}/rotate", response_model=AgentKeyCreated)
async def rotate_agent_api_key(
    key_id: UUID, request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> AgentKeyCreated:
    secret = generate_agent_key()
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        old = await session.scalar(
            select(AgentAPIKeyRecord)
            .where(
                AgentAPIKeyRecord.key_id == key_id,
                AgentAPIKeyRecord.workspace_id == context.workspace_id,
            )
            .with_for_update()
        )
        if old is None:
            raise HTTPException(status_code=404, detail="Agent API key not found")
        old.revoked_at = now
        record = AgentAPIKeyRecord(
            key_id=uuid4(),
            workspace_id=context.workspace_id,
            key_hash=hash_agent_key(secret),
            key_prefix=key_prefix(secret),
            name=old.name,
            created_at=now,
            last_used_at=None,
            revoked_at=None,
        )
        session.add(record)
    return AgentKeyCreated(**_view(record).model_dump(), secret=secret)


@router.post("/strategy-assessments", response_model=StrategyAssessmentResult)
async def strategy_assessment(
    payload: StrategyAssessmentRequest,
    request: Request,
    key: AgentKeyContext = Depends(require_agent_key),
) -> StrategyAssessmentResult:
    database = request.app.state.database
    async with database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, key.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    policy = AssessmentPolicy.from_payload(workspace.assessment_policy)
    projections = PostgresBrokerProjectionStore(database.sessions, key.workspace_id)
    status = await projections.get_status()
    account = await projections.get_account()
    now = datetime.now(UTC)
    broker_ready = _broker_evidence_is_ready(
        status,
        account,
        now=now,
        maximum_age=timedelta(seconds=policy.execution_max_quote_age_seconds),
    )
    return assess_strategy(
        payload,
        policy,
        paper_equity=account.equity if account and broker_ready else None,
        broker_evidence_available=broker_ready,
        policy_updated_at=workspace.updated_at,
    )
