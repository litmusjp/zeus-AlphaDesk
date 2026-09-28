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
from packages.connected.external_account import ExternalAccountProvider
from packages.connected.strategy_assessment import (
    StrategyAssessmentRequest,
    StrategyAssessmentResult,
    assess_strategy,
)
from packages.database.models import AgentAPIKeyRecord, ConnectedOpportunityRecord, WorkspaceRecord
from packages.domain.broker import BrokerAccount, BrokerSyncStatus
from packages.domain.system import BrokerState
from packages.domain.workflow import CatalystFeatures, NoTrade, Signal, TradeIdea
from packages.security.agent_keys import generate_agent_key, hash_agent_key, key_prefix
from packages.strategy.catalyst import CatalystMomentumStrategy, score_signal

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
    if payload.is_external:
        provider: ExternalAccountProvider | None = getattr(
            request.app.state, "external_account_provider", None
        )
        external_evidence = None
        if payload.scope != "SIGNAL_QUALITY" and provider is not None:
            try:
                candidate = await provider.get_account(
                    workspace_id=key.workspace_id,
                    key_id=key.key_id,
                    account_id=payload.external_account_id or "",
                    sandbox_id=payload.external_sandbox_id or "",
                    environment=payload.external_environment or "",
                )
                if (
                    candidate is not None
                    and candidate.account_id == payload.external_account_id
                    and candidate.sandbox_id == payload.external_sandbox_id
                    and candidate.environment == "PAPER"
                ):
                    evidence_age = datetime.now(UTC) - candidate.fetched_at
                    if timedelta(0) <= evidence_age <= timedelta(
                        seconds=policy.execution_max_quote_age_seconds
                    ):
                        external_evidence = candidate
            except Exception:
                # Provider failures are deliberately indistinguishable from absent evidence.
                external_evidence = None
        trusted_features: CatalystFeatures | None = None
        trusted_observed_at: datetime | None = None
        trusted_signal_direction: str | None = None
        if payload.scope == "SIGNAL_QUALITY" or external_evidence is not None:
            now = datetime.now(UTC)
            try:
                async with database.sessions() as session:
                    opportunity = await session.scalar(
                        select(ConnectedOpportunityRecord)
                        .where(
                            ConnectedOpportunityRecord.workspace_id == key.workspace_id,
                            ConnectedOpportunityRecord.symbol == payload.underlying_symbol.upper(),
                            ConnectedOpportunityRecord.source == "ALPACA_REAL",
                            ConnectedOpportunityRecord.observed_at <= now,
                            ConnectedOpportunityRecord.expires_at >= now,
                        )
                        .order_by(ConnectedOpportunityRecord.observed_at.desc())
                    )
                if (
                    opportunity is not None
                    and now >= opportunity.observed_at
                    and now < opportunity.expires_at
                    and getattr(opportunity, "source", None) == "ALPACA_REAL"
                    and getattr(opportunity, "symbol", None) == payload.underlying_symbol.upper()
                    and getattr(opportunity, "state", None) in {
                        "NO_TRADE", "TRADE", "PRE_SCAN_CANDIDATE", "RESEARCH_CANDIDATE",
                    }
                ):
                    raw_signal = opportunity.payload.get("signal", {})
                    signal = Signal.model_validate(raw_signal)
                    signal_features = CatalystFeatures.model_validate(signal.features)
                    signal_age = now - signal.observed_at
                    if (
                        signal.symbol == payload.underlying_symbol.upper()
                        and signal.source_versions
                        and signal.observed_at == opportunity.observed_at
                        and (
                            payload.scope != "SIGNAL_QUALITY"
                            or signal.score == score_signal(signal_features)
                        )
                        and timedelta(0) <= signal_age <= timedelta(
                            seconds=policy.execution_max_quote_age_seconds
                        )
                    ):
                        if payload.scope == "SIGNAL_QUALITY":
                            evaluated = CatalystMomentumStrategy(
                                minimum_score=policy.minimum_signal_score,
                                maximum_gap=policy.maximum_gap_percent,
                                minimum_catalyst_confidence=policy.minimum_catalyst_confidence,
                            ).evaluate_signal(signal)
                            if isinstance(evaluated, NoTrade):
                                if opportunity.state != "NO_TRADE":
                                    raise ValueError(
                                        "opportunity state does not match evaluated signal"
                                    )
                                trusted_signal_direction = "NO_TRADE"
                            else:
                                if opportunity.state == "NO_TRADE":
                                    raise ValueError("trade signal cannot have NO_TRADE state")
                                idea = TradeIdea.model_validate(
                                    opportunity.payload.get("trade_idea")
                                )
                                if (
                                    idea.signal_id != signal.signal_id
                                    or idea.signal_id != evaluated.signal_id
                                    or idea.symbol != payload.underlying_symbol.upper()
                                    or idea.direction != evaluated.direction
                                ):
                                    raise ValueError(
                                        "signal trade idea does not match evaluated signal"
                                    )
                                trusted_signal_direction = idea.direction.value
                        trusted_features = signal_features
                        trusted_observed_at = signal.observed_at
            except Exception:
                trusted_features = None
                trusted_observed_at = None
                trusted_signal_direction = None
        return assess_strategy(
            payload,
            policy,
            paper_equity=external_evidence.equity if external_evidence else None,
            broker_evidence_available=external_evidence is not None,
            policy_updated_at=workspace.updated_at,
            trusted_market_features=trusted_features,
            trusted_score_source=(
                "alphadesk_connected_opportunity" if trusted_features is not None else None
            ),
            trusted_score_observed_at=trusted_observed_at,
            trusted_signal_direction=trusted_signal_direction,
        )
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
