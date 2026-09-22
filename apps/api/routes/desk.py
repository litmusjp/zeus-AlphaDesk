from __future__ import annotations

# FastAPI dependencies are intentionally declared in parameter defaults.
# ruff: noqa: B008
import asyncio
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from anthropic import APITimeoutError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import Select, delete, or_, select
from sqlalchemy.exc import IntegrityError

from packages.ai.provider import (
    AIProvider,
    AnthropicProvider,
    OpenRouterProvider,
    StructuredOutputError,
)
from packages.ai.store import AIWorkflowStore
from packages.ai.watchlist import (
    DISCOVERY_UNIVERSE,
    WatchlistResearchReport,
    run_watchlist_research,
)
from packages.ai.workflow import AIWorkflow
from packages.auth.dependencies import WorkspaceContext, require_workspace
from packages.broker.alpaca_adapter import AlpacaBrokerAdapter, AlpacaPaperBrokerAdapter
from packages.broker.projections import PostgresBrokerProjectionStore
from packages.connected.assessment_policy import (
    ASSESSMENT_PROFILE_LABELS,
    AssessmentPolicy,
    AssessmentProfile,
    materialize_profile,
    profile_for_policy,
)
from packages.connected.market_clock import AlpacaMarketClockAdapter, ConnectedMarketClock
from packages.connected.opportunities import (
    ConnectedAnalysis,
    ConnectedOpportunityService,
    complete_scan_run,
    start_scan_run,
)
from packages.connected.option_scan_policy import ScanMode
from packages.database.models import (
    AIWorkflowRunRecord,
    AuditRecord,
    BrokerAccountRecord,
    BrokerOrderRecord,
    BrokerPositionRecord,
    BrokerSyncStateRecord,
    ConditionalApprovalRecord,
    ConnectedOpportunityRecord,
    ConnectedScanRunRecord,
    GuardianIncidentRecord,
    WatchlistSymbolRecord,
    WorkspaceCredentialRecord,
    WorkspaceRecord,
)
from packages.domain.ai import AIWorkflowResult, Citation
from packages.domain.broker import BrokerAccount, BrokerOrder, BrokerPosition, BrokerSyncStatus
from packages.domain.guardian import GuardianStatus, GuardianTrigger
from packages.domain.system import BrokerState, TradingEnvironment
from packages.domain.workflow import OrderIntent, RankedCandidate, RiskDecision, RiskDecisionValue
from packages.execution.conditional_approval import (
    ApprovalState,
    ExitOrderSide,
    approval_can_be_renewed,
    approval_is_active,
    candidate_structure_identity,
    order_structure_fingerprint,
    validate_exit_plan,
)
from packages.execution.connected_paper import execute_connected_paper_order
from packages.execution.engine import SubmissionUncertain
from packages.execution.intents import create_order_intent
from packages.guardian.store import PostgresGuardianStore
from packages.observability.logging import get_logger
from packages.security.store import CredentialStore
from packages.trading.environment import LIVE_CONFIRMATION_PHRASE

router = APIRouter(prefix="/desk", tags=["connected-paper"])
logger = get_logger(__name__)


class WorkspaceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    mode: Literal["CONNECTED_PAPER"] = "CONNECTED_PAPER"
    trading_environment: TradingEnvironment
    status: str
    scanner_enabled: bool
    watchlist_count: int


class AssessmentPolicyInput(AssessmentPolicy):
    model_config = ConfigDict(frozen=True)

    profile: AssessmentProfile | None = None


class AssessmentProfileOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: AssessmentProfile
    label: str


class AssessmentPolicyView(AssessmentPolicy):
    model_config = ConfigDict(frozen=True)

    updated_at: datetime | None = None
    active_profile: str
    available_profiles: tuple[AssessmentProfileOption, ...]


def _assessment_policy_view(
    policy: AssessmentPolicy, updated_at: datetime | None
) -> AssessmentPolicyView:
    profile = profile_for_policy(policy)
    return AssessmentPolicyView(
        **policy.model_dump(),
        updated_at=updated_at,
        active_profile=profile.value if profile is not None else "CUSTOM",
        available_profiles=tuple(
            AssessmentProfileOption(value=item, label=ASSESSMENT_PROFILE_LABELS[item])
            for item in AssessmentProfile
        ),
    )


class CredentialStatusView(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    configured: bool
    enabled: bool
    validation_status: str
    fingerprint: str | None
    configuration: dict[str, Any]
    validated_at: datetime | None
    updated_at: datetime | None


class AlpacaCredentialInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key_id: SecretStr = Field(min_length=8)
    secret_key: SecretStr = Field(min_length=8)


ALPACA_PAPER_PROVIDER = "ALPACA_PAPER"
ALPACA_LIVE_PROVIDER = "ALPACA_LIVE"


class OpenRouterCredentialInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: SecretStr = Field(min_length=8)
    model: str = Field(min_length=2, max_length=160)


class AnthropicCredentialInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: SecretStr = Field(min_length=8)
    model: str = Field(min_length=2, max_length=160)


class ProviderTestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    status: str
    detail: str
    account_status: str | None = None
    model: str | None = None


class ProbeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"]
    citation: Citation


MAX_WATCHLIST_SYMBOLS = 50


class WatchlistInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbols: tuple[str, ...] = Field(max_length=MAX_WATCHLIST_SYMBOLS)
    source: Literal["operator", "ai_research", "default_restore"] = "operator"


class WatchlistRemovalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbols: tuple[str, ...] = Field(max_length=MAX_WATCHLIST_SYMBOLS)


class ScannerInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool


class ScannerFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    code: Literal["REAL_DATA_UNAVAILABLE"] = "REAL_DATA_UNAVAILABLE"
    detail: str | None = None


class ScannerResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    scan_run_id: UUID
    trigger: str
    started_at: datetime
    completed_at: datetime
    attempted: int
    results: tuple[ConnectedAnalysis, ...]
    failures: tuple[ScannerFailure, ...]


class WatchlistResearchView(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    scan_run_id: UUID
    scan_completed_at: datetime | None
    researched_at: datetime
    universe: tuple[str, ...]
    report: WatchlistResearchReport


class ScanRunView(BaseModel):
    model_config = ConfigDict(frozen=True)

    scan_run_id: UUID
    trigger: str
    source: str
    started_at: datetime
    completed_at: datetime | None
    attempted: int
    completed: int
    failed: int


class ConfirmPaperOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str = Field(min_length=10, max_length=64)


class ExitPlanInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    stop_loss: Decimal = Field(gt=0)
    profit_target: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime
    stale_data_behavior: Literal["FAIL_CLOSED"]


class ConditionalApprovalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_limit_price: Decimal | None = Field(default=None, gt=0)
    max_loss: Decimal | None = Field(default=None, gt=0)
    max_quantity: int | None = Field(default=None, ge=1, le=100)
    max_quote_age_seconds: int = Field(default=30, ge=1, le=300)
    live_order_confirmation: str | None = None
    exit_plan: ExitPlanInput | None = None


class ConditionalExitApprovalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    limit_price_bound: Decimal | None = Field(default=None, gt=0)
    max_quote_age_seconds: int = Field(default=30, ge=1, le=300)


class ConditionalApprovalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: UUID
    opportunity_id: UUID | None
    approval_kind: Literal["OPEN", "CLOSE"]
    symbol: str
    state: str
    session_date: date
    approved_at: datetime
    expires_at: datetime
    client_order_id: str
    structure_fingerprint: str
    max_limit_price: Decimal
    max_loss: Decimal
    max_quantity: int
    max_quote_age_seconds: int
    min_limit_price: Decimal | None
    position_asset_id: str | None
    position_side: str | None
    exit_order_side: str | None
    broker_order_id: str | None
    failure_reason: str | None
    exit_plan: dict[str, Any] | None


def _credential_store(request: Request) -> CredentialStore:
    cipher = request.app.state.credential_cipher
    if cipher is None:
        raise HTTPException(
            status_code=503, detail="Encrypted credential storage is not configured"
        )
    return CredentialStore(request.app.state.database.sessions, cipher)


async def _workspace_environment(request: Request, workspace_id: UUID) -> TradingEnvironment:
    sessions = request.app.state.database.sessions
    if not callable(sessions):
        # Unit-level request doubles from the paper-only close path predate the
        # persisted environment. Production sessions are always callable.
        return TradingEnvironment.PAPER
    async with sessions() as session:
        workspace = await session.get(WorkspaceRecord, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return TradingEnvironment(workspace.trading_environment)
    except ValueError as error:
        raise HTTPException(
            status_code=409, detail="Workspace trading environment is invalid"
        ) from error


def _alpaca_provider(environment: TradingEnvironment) -> str:
    return ALPACA_LIVE_PROVIDER if environment is TradingEnvironment.LIVE else ALPACA_PAPER_PROVIDER


def _credential_view(
    provider: str, record: WorkspaceCredentialRecord | None
) -> CredentialStatusView:
    if record is None:
        return CredentialStatusView(
            provider=provider,
            configured=False,
            enabled=False,
            validation_status="NOT_CONFIGURED",
            fingerprint=None,
            configuration={},
            validated_at=None,
            updated_at=None,
        )
    return CredentialStatusView(
        provider=provider,
        configured=True,
        enabled=record.enabled,
        validation_status=record.validation_status,
        fingerprint=record.fingerprint,
        configuration=record.configuration,
        validated_at=record.validated_at,
        updated_at=record.updated_at,
    )


def _conditional_approval_view(
    record: ConditionalApprovalRecord, symbol: str | None
) -> ConditionalApprovalView:
    return ConditionalApprovalView(
        approval_id=record.approval_id,
        opportunity_id=record.opportunity_id,
        approval_kind=record.approval_kind,
        symbol=symbol or record.position_symbol or "UNKNOWN",
        state=record.state,
        session_date=record.session_date,
        approved_at=record.approved_at,
        expires_at=record.expires_at,
        client_order_id=record.client_order_id,
        structure_fingerprint=record.structure_fingerprint,
        max_limit_price=record.max_limit_price,
        max_loss=record.max_loss,
        max_quantity=record.max_quantity,
        max_quote_age_seconds=record.max_quote_age_seconds,
        min_limit_price=record.min_limit_price,
        position_asset_id=record.position_asset_id,
        position_side=record.position_side,
        exit_order_side=record.exit_order_side,
        broker_order_id=record.broker_order_id,
        failure_reason=record.failure_reason,
        exit_plan=getattr(record, "exit_plan_payload", None),
    )


@router.get("/workspace", response_model=WorkspaceView)
async def workspace_view(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> WorkspaceView:
    async with request.app.state.database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, context.workspace_id)
        assert workspace is not None
        watchlist_count = len(
            tuple(
                await session.scalars(
                    select(WatchlistSymbolRecord.symbol).where(
                        WatchlistSymbolRecord.workspace_id == context.workspace_id
                    )
                )
            )
        )
    return WorkspaceView(
        workspace_id=context.workspace_id,
        status=workspace.status,
        trading_environment=TradingEnvironment(workspace.trading_environment),
        scanner_enabled=workspace.scanner_enabled,
        watchlist_count=watchlist_count,
    )


@router.get("/assessment-policy", response_model=AssessmentPolicyView)
async def get_assessment_policy(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> AssessmentPolicyView:
    async with request.app.state.database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, context.workspace_id)
        assert workspace is not None
        policy = AssessmentPolicy.from_payload(workspace.assessment_policy)
        updated_at = workspace.updated_at
    return _assessment_policy_view(policy, updated_at)


@router.put("/assessment-policy", response_model=AssessmentPolicyView)
async def update_assessment_policy(
    payload: AssessmentPolicyInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> AssessmentPolicyView:
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        workspace = await session.get(WorkspaceRecord, context.workspace_id, with_for_update=True)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        pending_approvals = list(
            await session.scalars(
                select(ConditionalApprovalRecord).where(
                    ConditionalApprovalRecord.workspace_id == context.workspace_id,
                    ConditionalApprovalRecord.approval_kind == "OPEN",
                    ConditionalApprovalRecord.state.in_(
                        (
                            ApprovalState.REVALIDATING,
                            ApprovalState.READY_TO_SUBMIT,
                            ApprovalState.SUBMITTING,
                            ApprovalState.DISPATCH_AUTHORIZED,
                            ApprovalState.SUBMISSION_UNCERTAIN,
                        )
                    ),
                )
            )
        )
        if pending_approvals:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Wait for active paper submissions to finish before changing "
                    "assessment settings"
                ),
            )
        policy = (
            materialize_profile(payload.profile)
            if payload.profile is not None
            else AssessmentPolicy.model_validate(payload.model_dump(exclude={"profile"}))
        )
        active_profile = profile_for_policy(policy)
        workspace.assessment_policy = {
            "profile": active_profile.value if active_profile is not None else "CUSTOM",
            **policy.model_dump(mode="json"),
        }
        workspace.updated_at = now
        active_approvals = list(
            await session.scalars(
                select(ConditionalApprovalRecord).where(
                    ConditionalApprovalRecord.workspace_id == context.workspace_id,
                    ConditionalApprovalRecord.approval_kind == "OPEN",
                    ConditionalApprovalRecord.state.in_(
                        (
                            ApprovalState.APPROVED_FOR_SESSION,
                            ApprovalState.REVALIDATING,
                            ApprovalState.READY_TO_SUBMIT,
                            ApprovalState.SUBMITTING,
                            ApprovalState.DISPATCH_AUTHORIZED,
                            ApprovalState.SUBMISSION_UNCERTAIN,
                        )
                    ),
                )
            )
        )
        for approval in active_approvals:
            session.add(
                AuditRecord(
                    audit_id=uuid4(),
                    workspace_id=context.workspace_id,
                    actor_user_id=context.principal.user_id,
                    action="CONDITIONAL_APPROVAL_INVALIDATED_POLICY_UPDATE",
                    detail={"approval_id": str(approval.approval_id)},
                    occurred_at=now,
                )
            )
        for approval in active_approvals:
            approval.state = ApprovalState.CONDITION_FAILED
            approval.failure_reason = "assessment_policy_updated"
            approval.updated_at = now
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                workspace_id=context.workspace_id,
                actor_user_id=context.principal.user_id,
                action="ASSESSMENT_POLICY_UPDATED",
                detail={
                    "profile": active_profile.value if active_profile is not None else "CUSTOM",
                    "policy": policy.model_dump(mode="json"),
                },
                occurred_at=now,
            )
        )
    return _assessment_policy_view(policy, now)


@router.get("/credentials", response_model=list[CredentialStatusView])
async def credential_statuses(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> list[CredentialStatusView]:
    async with request.app.state.database.sessions() as session:
        records = {
            record.provider: record
            for record in await session.scalars(
                select(WorkspaceCredentialRecord).where(
                    WorkspaceCredentialRecord.workspace_id == context.workspace_id
                )
            )
        }
    return [
        _credential_view(provider, records.get(provider))
        for provider in (ALPACA_PAPER_PROVIDER, ALPACA_LIVE_PROVIDER, "OPENROUTER", "ANTHROPIC")
    ]


async def _test_alpaca(
    payload: AlpacaCredentialInput, environment: TradingEnvironment
) -> ProviderTestResult:
    provider = (
        ALPACA_LIVE_PROVIDER if environment is TradingEnvironment.LIVE else ALPACA_PAPER_PROVIDER
    )
    adapter = AlpacaBrokerAdapter(
        payload.api_key_id.get_secret_value(),
        payload.secret_key.get_secret_value(),
        environment=environment,
    )
    try:
        snapshot = await asyncio.wait_for(adapter.reconcile(), timeout=20)
        return ProviderTestResult(
            provider=provider,
            status="VERIFIED",
            detail=(
                f"Alpaca {environment.value.lower()} endpoint authenticated; account, "
                "positions, and orders reconciled."
            ),
            account_status=snapshot.account.status,
        )
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"Alpaca paper validation failed ({type(error).__name__})",
        ) from error
    finally:
        await adapter.close()


@router.post("/credentials/alpaca/test", response_model=ProviderTestResult)
async def test_alpaca_credentials(
    payload: AlpacaCredentialInput,
    _: WorkspaceContext = Depends(require_workspace),
) -> ProviderTestResult:
    return await _test_alpaca(payload, TradingEnvironment.PAPER)


@router.put("/credentials/alpaca", response_model=CredentialStatusView)
async def save_alpaca_credentials(
    payload: AlpacaCredentialInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> CredentialStatusView:
    await _test_alpaca(payload, TradingEnvironment.PAPER)
    record = await _credential_store(request).save(
        workspace_id=context.workspace_id,
        actor_user_id=context.principal.user_id,
        provider=ALPACA_PAPER_PROVIDER,
        secret_payload={
            "api_key_id": payload.api_key_id.get_secret_value(),
            "secret_key": payload.secret_key.get_secret_value(),
        },
        configuration={"endpoint": TradingEnvironment.PAPER.value},
        validation_status="VERIFIED",
        enabled=True,
    )
    async with request.app.state.database.sessions.begin() as session:
        workspace = await session.get(WorkspaceRecord, context.workspace_id)
        assert workspace is not None
        workspace.status = "CONNECTING"
        workspace.updated_at = datetime.now(UTC)
    return _credential_view(ALPACA_PAPER_PROVIDER, record)


@router.post("/credentials/alpaca-live/test", response_model=ProviderTestResult)
async def test_alpaca_live_credentials(
    payload: AlpacaCredentialInput,
    _: WorkspaceContext = Depends(require_workspace),
) -> ProviderTestResult:
    return await _test_alpaca(payload, TradingEnvironment.LIVE)


@router.put("/credentials/alpaca-live", response_model=CredentialStatusView)
async def save_alpaca_live_credentials(
    payload: AlpacaCredentialInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> CredentialStatusView:
    await _test_alpaca(payload, TradingEnvironment.LIVE)
    record = await _credential_store(request).save(
        workspace_id=context.workspace_id,
        actor_user_id=context.principal.user_id,
        provider=ALPACA_LIVE_PROVIDER,
        secret_payload={
            "api_key_id": payload.api_key_id.get_secret_value(),
            "secret_key": payload.secret_key.get_secret_value(),
        },
        configuration={"endpoint": TradingEnvironment.LIVE.value},
        validation_status="VERIFIED",
        enabled=True,
    )
    return _credential_view(ALPACA_LIVE_PROVIDER, record)


async def _test_openrouter(payload: OpenRouterCredentialInput) -> ProviderTestResult:
    provider = OpenRouterProvider(
        payload.api_key.get_secret_value(), model=payload.model, timeout_seconds=20
    )
    try:
        await provider.generate(
            agent_name="AlphaDeskCapabilityProbe",
            instructions="Return status ok and cite the supplied system source. No tools.",
            input_payload='Source: {"source_id":"alphadesk-system","claim":"capability probe"}',
            response_model=ProbeResponse,
        )
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"OpenRouter model capability probe failed ({type(error).__name__})",
        ) from error
    return ProviderTestResult(
        provider="OPENROUTER",
        status="VERIFIED",
        detail="The model produced a schema-valid read-only response.",
        model=payload.model,
    )


async def _test_anthropic(payload: AnthropicCredentialInput) -> ProviderTestResult:
    provider = AnthropicProvider(
        payload.api_key.get_secret_value(), model=payload.model, timeout_seconds=20
    )
    try:
        await provider.generate(
            agent_name="AlphaDeskCapabilityProbe",
            instructions="Return status ok and cite the supplied system source. No tools.",
            input_payload='Source: {"source_id":"alphadesk-system","claim":"capability probe"}',
            response_model=ProbeResponse,
        )
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"Anthropic model capability probe failed ({type(error).__name__})",
        ) from error
    return ProviderTestResult(
        provider="ANTHROPIC",
        status="VERIFIED",
        detail="The model produced a schema-valid read-only response.",
        model=payload.model,
    )


async def _activate_ai_provider(request: Request, workspace_id: UUID, provider: str) -> None:
    async with request.app.state.database.sessions.begin() as session:
        records = list(
            await session.scalars(
                select(WorkspaceCredentialRecord).where(
                    WorkspaceCredentialRecord.workspace_id == workspace_id,
                    WorkspaceCredentialRecord.provider.in_(("OPENROUTER", "ANTHROPIC")),
                )
            )
        )
        for record in records:
            configuration = dict(record.configuration or {})
            configuration["active"] = record.provider == provider
            record.configuration = configuration


@router.post("/credentials/openrouter/test", response_model=ProviderTestResult)
async def test_openrouter_credentials(
    payload: OpenRouterCredentialInput,
    _: WorkspaceContext = Depends(require_workspace),
) -> ProviderTestResult:
    return await _test_openrouter(payload)


@router.put("/credentials/openrouter", response_model=CredentialStatusView)
async def save_openrouter_credentials(
    payload: OpenRouterCredentialInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> CredentialStatusView:
    await _test_openrouter(payload)
    record = await _credential_store(request).save(
        workspace_id=context.workspace_id,
        actor_user_id=context.principal.user_id,
        provider="OPENROUTER",
        secret_payload={"api_key": payload.api_key.get_secret_value()},
        configuration={"model": payload.model, "compatibility": "verified"},
        validation_status="VERIFIED",
        enabled=True,
    )
    await _activate_ai_provider(request, context.workspace_id, "OPENROUTER")
    return _credential_view("OPENROUTER", record)


@router.post("/credentials/anthropic/test", response_model=ProviderTestResult)
async def test_anthropic_credentials(
    payload: AnthropicCredentialInput,
    _: WorkspaceContext = Depends(require_workspace),
) -> ProviderTestResult:
    return await _test_anthropic(payload)


@router.put("/credentials/anthropic", response_model=CredentialStatusView)
async def save_anthropic_credentials(
    payload: AnthropicCredentialInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> CredentialStatusView:
    await _test_anthropic(payload)
    record = await _credential_store(request).save(
        workspace_id=context.workspace_id,
        actor_user_id=context.principal.user_id,
        provider="ANTHROPIC",
        secret_payload={"api_key": payload.api_key.get_secret_value()},
        configuration={"model": payload.model, "compatibility": "native_tool_schema"},
        validation_status="VERIFIED",
        enabled=True,
    )
    await _activate_ai_provider(request, context.workspace_id, "ANTHROPIC")
    return _credential_view("ANTHROPIC", record)


@router.delete("/credentials/{provider}", status_code=204)
async def delete_credentials(
    provider: Literal["alpaca", "alpaca-live", "openrouter", "anthropic"],
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> None:
    normalized = (
        ALPACA_LIVE_PROVIDER
        if provider == "alpaca-live"
        else (ALPACA_PAPER_PROVIDER if provider == "alpaca" else provider.upper())
    )
    await _credential_store(request).delete(
        workspace_id=context.workspace_id,
        actor_user_id=context.principal.user_id,
        provider=normalized,
    )
    if normalized in {ALPACA_PAPER_PROVIDER, "ALPACA"}:
        async with request.app.state.database.sessions.begin() as session:
            for model in (
                BrokerOrderRecord,
                BrokerPositionRecord,
                BrokerAccountRecord,
                BrokerSyncStateRecord,
                GuardianIncidentRecord,
            ):
                await session.execute(
                    delete(model).where(model.workspace_id == context.workspace_id)
                )
            workspace = await session.get(WorkspaceRecord, context.workspace_id)
            assert workspace is not None
            workspace.status = "ONBOARDING"
            workspace.scanner_enabled = False
            workspace.updated_at = datetime.now(UTC)


@router.get("/watchlist", response_model=list[str])
async def get_watchlist(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> list[str]:
    async with request.app.state.database.sessions() as session:
        return list(
            await session.scalars(
                select(WatchlistSymbolRecord.symbol)
                .where(WatchlistSymbolRecord.workspace_id == context.workspace_id)
                .order_by(WatchlistSymbolRecord.symbol)
            )
        )


@router.put("/watchlist", response_model=list[str])
async def replace_watchlist(
    payload: WatchlistInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> list[str]:
    symbols = tuple(sorted({item.strip().upper() for item in payload.symbols if item.strip()}))
    if len(symbols) > MAX_WATCHLIST_SYMBOLS or any(
        not item.isalnum() or len(item) > 16 for item in symbols
    ):
        raise HTTPException(status_code=422, detail="Watchlist contains invalid symbols")
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        await session.execute(
            select(WorkspaceRecord.workspace_id)
            .where(WorkspaceRecord.workspace_id == context.workspace_id)
            .with_for_update()
        )
        await session.execute(
            delete(WatchlistSymbolRecord).where(
                WatchlistSymbolRecord.workspace_id == context.workspace_id
            )
        )
        session.add_all(
            WatchlistSymbolRecord(workspace_id=context.workspace_id, symbol=symbol, created_at=now)
            for symbol in symbols
        )
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                workspace_id=context.workspace_id,
                actor_user_id=context.principal.user_id,
                action="WATCHLIST_REPLACED",
                detail={"symbols": list(symbols), "source": "operator"},
                occurred_at=now,
            )
        )
    return list(symbols)


@router.post("/watchlist", response_model=list[str])
async def add_to_watchlist(
    payload: WatchlistInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> list[str]:
    additions = tuple(sorted({item.strip().upper() for item in payload.symbols if item.strip()}))
    if any(not item.isalnum() or len(item) > 16 for item in additions):
        raise HTTPException(status_code=422, detail="Watchlist contains invalid symbols")
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        await session.execute(
            select(WorkspaceRecord.workspace_id)
            .where(WorkspaceRecord.workspace_id == context.workspace_id)
            .with_for_update()
        )
        existing = set(
            await session.scalars(
                select(WatchlistSymbolRecord.symbol).where(
                    WatchlistSymbolRecord.workspace_id == context.workspace_id
                )
            )
        )
        symbols = tuple(sorted(existing | set(additions)))
        if len(symbols) > MAX_WATCHLIST_SYMBOLS:
            raise HTTPException(
                status_code=422,
                detail=f"Watchlist cannot exceed {MAX_WATCHLIST_SYMBOLS} symbols",
            )
        new_symbols = set(symbols) - existing
        if new_symbols:
            session.add_all(
                WatchlistSymbolRecord(
                    workspace_id=context.workspace_id, symbol=symbol, created_at=now
                )
                for symbol in sorted(new_symbols)
            )
            session.add(
                AuditRecord(
                    audit_id=uuid4(),
                    workspace_id=context.workspace_id,
                    actor_user_id=context.principal.user_id,
                    action="WATCHLIST_SYMBOLS_ADDED",
                    detail={"symbols": sorted(new_symbols), "source": payload.source},
                    occurred_at=now,
                )
            )
    return list(symbols)


@router.post("/watchlist/remove", response_model=list[str])
async def remove_from_watchlist(
    payload: WatchlistRemovalInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> list[str]:
    removals = tuple(sorted({item.strip().upper() for item in payload.symbols if item.strip()}))
    if any(not item.isalnum() or len(item) > 16 for item in removals):
        raise HTTPException(status_code=422, detail="Watchlist contains invalid symbols")
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        await session.execute(
            select(WorkspaceRecord.workspace_id)
            .where(WorkspaceRecord.workspace_id == context.workspace_id)
            .with_for_update()
        )
        existing = set(
            await session.scalars(
                select(WatchlistSymbolRecord.symbol).where(
                    WatchlistSymbolRecord.workspace_id == context.workspace_id
                )
            )
        )
        removed = existing & set(removals)
        if removed:
            await session.execute(
                delete(WatchlistSymbolRecord).where(
                    WatchlistSymbolRecord.workspace_id == context.workspace_id,
                    WatchlistSymbolRecord.symbol.in_(removed),
                )
            )
            session.add(
                AuditRecord(
                    audit_id=uuid4(),
                    workspace_id=context.workspace_id,
                    actor_user_id=context.principal.user_id,
                    action="WATCHLIST_SYMBOLS_REMOVED",
                    detail={"symbols": sorted(removed), "source": "operator"},
                    occurred_at=now,
                )
            )
        symbols = tuple(sorted(existing - removed))
    return list(symbols)


@router.put("/scanner", response_model=WorkspaceView)
async def set_scanner(
    payload: ScannerInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> WorkspaceView:
    if payload.enabled:
        environment = await _workspace_environment(request, context.workspace_id)
        credential = await _credential_store(request).get(
            context.workspace_id, _alpaca_provider(environment)
        )
        if credential is None or not credential.enabled:
            raise HTTPException(
                status_code=409, detail="Verified target Alpaca credentials required"
            )
    async with request.app.state.database.sessions.begin() as session:
        workspace = await session.get(WorkspaceRecord, context.workspace_id, with_for_update=True)
        assert workspace is not None
        workspace.scanner_enabled = payload.enabled
        workspace.updated_at = datetime.now(UTC)
        count = len(
            tuple(
                await session.scalars(
                    select(WatchlistSymbolRecord.symbol).where(
                        WatchlistSymbolRecord.workspace_id == context.workspace_id
                    )
                )
            )
        )
        return WorkspaceView(
            workspace_id=workspace.workspace_id,
            status=workspace.status,
            trading_environment=TradingEnvironment(workspace.trading_environment),
            scanner_enabled=workspace.scanner_enabled,
            watchlist_count=count,
        )


async def _opportunity_service(
    request: Request, context: WorkspaceContext
) -> ConnectedOpportunityService:
    environment = await _workspace_environment(request, context.workspace_id)
    secrets = await _credential_store(request).reveal(
        context.workspace_id, _alpaca_provider(environment)
    )
    if secrets is None:
        raise HTTPException(status_code=409, detail="Verified target Alpaca credentials required")
    async with request.app.state.database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, context.workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        policy = AssessmentPolicy.from_payload(workspace.assessment_policy)
    return ConnectedOpportunityService(
        request.app.state.database.sessions,
        context.workspace_id,
        str(secrets["api_key_id"]),
        str(secrets["secret_key"]),
        policy=policy,
        environment=environment,
    )


@router.get("/market-clock", response_model=ConnectedMarketClock)
async def market_clock(
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> ConnectedMarketClock:
    environment = await _workspace_environment(request, context.workspace_id)
    secrets = await _credential_store(request).reveal(
        context.workspace_id, _alpaca_provider(environment)
    )
    if secrets is None:
        raise HTTPException(status_code=409, detail="Verified target Alpaca credentials required")
    try:
        return await AlpacaMarketClockAdapter(
            str(secrets["api_key_id"]),
            str(secrets["secret_key"]),
            environment=environment,
        ).get_clock()
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"Real market clock unavailable ({type(error).__name__})",
        ) from error


async def _resolve_scan_mode(
    request: Request,
    context: WorkspaceContext,
    requested: Literal["AUTO", "PRE_SCAN", "EXECUTION"],
) -> ScanMode:
    if requested == "PRE_SCAN":
        return ScanMode.PRE_SCAN
    if requested == "EXECUTION":
        return ScanMode.EXECUTION
    try:
        clock = await market_clock(request, context)
    except HTTPException:
        return ScanMode.EXECUTION
    return ScanMode.EXECUTION if clock.is_open else ScanMode.PRE_SCAN


@router.post("/opportunities/analyze/{symbol}", response_model=ConnectedAnalysis)
async def analyze_symbol(
    symbol: str,
    request: Request,
    scan_mode: Literal["AUTO", "PRE_SCAN", "EXECUTION"] = "AUTO",
    context: WorkspaceContext = Depends(require_workspace),
) -> ConnectedAnalysis:
    try:
        mode = await _resolve_scan_mode(request, context, scan_mode)
        return await (await _opportunity_service(request, context)).analyze(symbol, mode=mode)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"Real-data analysis unavailable ({type(error).__name__})",
        ) from error


@router.post("/scanner/scan", response_model=ScannerResult)
async def scan_now(
    request: Request,
    scan_mode: Literal["AUTO", "PRE_SCAN", "EXECUTION"] = "AUTO",
    context: WorkspaceContext = Depends(require_workspace),
) -> ScannerResult:
    async with request.app.state.database.sessions() as session:
        symbols = list(
            await session.scalars(
                select(WatchlistSymbolRecord.symbol)
                .where(WatchlistSymbolRecord.workspace_id == context.workspace_id)
                .order_by(WatchlistSymbolRecord.symbol)
            )
        )
    service = await _opportunity_service(request, context)
    mode = await _resolve_scan_mode(request, context, scan_mode)
    run = await start_scan_run(
        request.app.state.database.sessions,
        context.workspace_id,
        trigger="MANUAL",
        attempted_count=len(symbols),
    )
    results: list[ConnectedAnalysis] = []
    failures: list[ScannerFailure] = []
    for symbol in symbols:
        try:
            results.append(await service.analyze(symbol, scan_run_id=run.scan_run_id, mode=mode))
        except Exception as error:
            logger.warning(
                "workspace_scan_symbol_unavailable",
                extra={
                    "event": "workspace_scan_symbol_unavailable",
                    "workspace_id": str(context.workspace_id),
                    "symbol": symbol,
                    "failure_type": type(error).__name__,
                },
            )
            failures.append(ScannerFailure(symbol=symbol, detail=type(error).__name__))
    completed_run = await complete_scan_run(
        request.app.state.database.sessions,
        run.scan_run_id,
        completed_count=len(results),
        failed_count=len(failures),
    )
    assert completed_run.completed_at is not None
    dispositions = Counter(result.disposition for result in results)
    logger.info(
        "workspace_scan_completed",
        extra={
            "event": "workspace_scan_completed",
            "workspace_id": str(context.workspace_id),
            "scan_run_id": str(run.scan_run_id),
            "trigger": run.trigger,
            "attempted": len(symbols),
            "completed": len(results),
            "failed": len(failures),
            "dispositions": dict(dispositions),
        },
    )
    return ScannerResult(
        scan_run_id=run.scan_run_id,
        trigger=run.trigger,
        started_at=run.started_at,
        completed_at=completed_run.completed_at,
        attempted=len(symbols),
        results=tuple(results),
        failures=tuple(failures),
    )


@router.get("/scanner/runs", response_model=list[ScanRunView])
async def list_scan_runs(
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> list[ScanRunView]:
    async with request.app.state.database.sessions() as session:
        records = list(
            await session.scalars(
                select(ConnectedScanRunRecord)
                .where(
                    ConnectedScanRunRecord.workspace_id == context.workspace_id,
                    ConnectedScanRunRecord.trigger != "AI_RESEARCH",
                )
                .order_by(ConnectedScanRunRecord.started_at.desc())
                .limit(10)
            )
        )
    return [
        ScanRunView(
            scan_run_id=record.scan_run_id,
            trigger=record.trigger,
            source=record.source,
            started_at=record.started_at,
            completed_at=record.completed_at,
            attempted=record.attempted_count,
            completed=record.completed_count,
            failed=record.failed_count,
        )
        for record in records
    ]


@router.get("/scanner/runs/{scan_run_id}", response_model=list[ConnectedAnalysis])
async def get_scan_run_results(
    scan_run_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> list[ConnectedAnalysis]:
    async with request.app.state.database.sessions() as session:
        owned_run = await session.scalar(
            select(ConnectedScanRunRecord.scan_run_id).where(
                ConnectedScanRunRecord.scan_run_id == scan_run_id,
                ConnectedScanRunRecord.workspace_id == context.workspace_id,
                ConnectedScanRunRecord.trigger != "AI_RESEARCH",
            )
        )
        if owned_run is None:
            raise HTTPException(status_code=404, detail="Scan run not found")
        records = list(
            await session.scalars(
                select(ConnectedOpportunityRecord)
                .where(
                    ConnectedOpportunityRecord.workspace_id == context.workspace_id,
                    ConnectedOpportunityRecord.scan_run_id == scan_run_id,
                )
                .order_by(ConnectedOpportunityRecord.symbol)
            )
        )
    return [ConnectedAnalysis.model_validate(record.payload) for record in records]


async def _save_watchlist_ai_run(
    request: Request,
    context: WorkspaceContext,
    *,
    provider: str,
    model: str,
    input_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any],
    degraded: bool,
    failure_reason: str | None,
) -> None:
    async with request.app.state.database.sessions.begin() as session:
        session.add(
            AIWorkflowRunRecord(
                run_id=uuid4(),
                workspace_id=context.workspace_id,
                correlation_id=uuid4(),
                provider=provider,
                model=model,
                prompt_versions={"watchlist_research": "watchlist-research-v1"},
                schema_version=1,
                input_payload=input_payload,
                output_payload=output_payload,
                degraded=degraded,
                failure_reason=failure_reason,
                created_at=datetime.now(UTC),
            )
        )


async def _load_watchlist_ai_provider(
    request: Request,
    context: WorkspaceContext,
    *,
    timeout_seconds: int,
) -> tuple[str, str, AIProvider]:
    credential_store = _credential_store(request)
    async with request.app.state.database.sessions() as session:
        credentials = list(
            await session.scalars(
                select(WorkspaceCredentialRecord).where(
                    WorkspaceCredentialRecord.workspace_id == context.workspace_id,
                    WorkspaceCredentialRecord.provider.in_(("OPENROUTER", "ANTHROPIC")),
                    WorkspaceCredentialRecord.enabled.is_(True),
                    WorkspaceCredentialRecord.validation_status == "VERIFIED",
                )
            )
        )
    credential = next(
        (item for item in credentials if bool(item.configuration.get("active"))),
        next((item for item in credentials if item.provider == "OPENROUTER"), None),
    )
    if credential is None:
        raise HTTPException(status_code=409, detail="Verified AI provider credentials required")
    secrets = await credential_store.reveal(context.workspace_id, credential.provider)
    if secrets is None:
        raise HTTPException(status_code=409, detail="Verified AI provider credentials required")
    model = str(credential.configuration.get("model", ""))
    provider = (
        AnthropicProvider(
            str(secrets["api_key"]),
            model=model,
            timeout_seconds=timeout_seconds,
        )
        if credential.provider == "ANTHROPIC"
        else OpenRouterProvider(
            str(secrets["api_key"]),
            model=model,
            timeout_seconds=timeout_seconds,
        )
    )
    return credential.provider, model, provider


@router.post("/watchlist/research", response_model=WatchlistResearchView)
async def research_watchlist(
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> WatchlistResearchView:
    async with request.app.state.database.sessions() as session:
        current_symbols = tuple(
            await session.scalars(
                select(WatchlistSymbolRecord.symbol)
                .where(WatchlistSymbolRecord.workspace_id == context.workspace_id)
                .order_by(WatchlistSymbolRecord.symbol)
            )
        )
    symbols = tuple(dict.fromkeys((*current_symbols, *DISCOVERY_UNIVERSE)))
    base_input_payload = {"universe": symbols}
    watchlist_ai_timeout = min(max(request.app.state.settings.ai_timeout_seconds, 60), 120)
    try:
        provider_name, model, provider = await _load_watchlist_ai_provider(
            request,
            context,
            timeout_seconds=watchlist_ai_timeout,
        )
    except HTTPException:
        await _save_watchlist_ai_run(
            request,
            context,
            provider="UNAVAILABLE",
            model="",
            input_payload=base_input_payload,
            output_payload={"degraded": True, "failure_reason": "credentials_unavailable"},
            degraded=True,
            failure_reason="credentials_unavailable",
        )
        raise
    except Exception as error:
        failure_reason = type(error).__name__
        await _save_watchlist_ai_run(
            request,
            context,
            provider="UNAVAILABLE",
            model="",
            input_payload=base_input_payload,
            output_payload={"degraded": True, "failure_reason": failure_reason},
            degraded=True,
            failure_reason=failure_reason,
        )
        raise HTTPException(status_code=503, detail="AI provider is unavailable") from error
    try:
        service = await _opportunity_service(request, context)
    except HTTPException:
        await _save_watchlist_ai_run(
            request,
            context,
            provider=provider_name,
            model=model,
            input_payload=base_input_payload,
            output_payload={"degraded": True, "failure_reason": "market_data_unavailable"},
            degraded=True,
            failure_reason="market_data_unavailable",
        )
        raise
    except Exception as error:
        failure_reason = type(error).__name__
        await _save_watchlist_ai_run(
            request,
            context,
            provider=provider_name,
            model=model,
            input_payload=base_input_payload,
            output_payload={"degraded": True, "failure_reason": failure_reason},
            degraded=True,
            failure_reason=failure_reason,
        )
        raise HTTPException(status_code=503, detail="Market data is unavailable") from error
    research_run = await start_scan_run(
        request.app.state.database.sessions,
        context.workspace_id,
        trigger="AI_RESEARCH",
        attempted_count=len(symbols),
    )
    results: list[ConnectedAnalysis] = []
    failures: list[ScannerFailure] = []
    settled_symbols: set[str] = set()
    try:
        async with asyncio.timeout(300):
            for symbol in symbols:
                try:
                    results.append(
                        await asyncio.wait_for(
                            service.analyze(
                                symbol,
                                scan_run_id=research_run.scan_run_id,
                                mode=ScanMode.PRE_SCAN,
                                create_intent=True,
                            ),
                            timeout=30,
                        )
                    )
                except Exception as error:
                    logger.warning(
                        "watchlist_research_symbol_unavailable",
                        extra={
                            "event": "watchlist_research_symbol_unavailable",
                            "workspace_id": str(context.workspace_id),
                            "symbol": symbol,
                            "failure_type": type(error).__name__,
                        },
                    )
                    failures.append(ScannerFailure(symbol=symbol, detail=type(error).__name__))
                    settled_symbols.add(symbol)
                else:
                    settled_symbols.add(symbol)
    except TimeoutError:
        failures.extend(
            ScannerFailure(symbol=symbol, detail="RESEARCH_TIMEOUT")
            for symbol in symbols
            if symbol not in settled_symbols
        )
    finally:
        try:
            completed_run = await complete_scan_run(
                request.app.state.database.sessions,
                research_run.scan_run_id,
                completed_count=len(results),
                failed_count=len(failures),
            )
        except Exception as error:
            logger.exception(
                "watchlist_research_scan_finalize_failed",
                extra={
                    "event": "watchlist_research_scan_finalize_failed",
                    "workspace_id": str(context.workspace_id),
                    "scan_run_id": str(research_run.scan_run_id),
                    "failure_type": type(error).__name__,
                },
            )
            raise HTTPException(
                status_code=503,
                detail="Research scan could not be finalized",
            ) from error

    by_symbol = {result.symbol: result for result in results}
    evidence = tuple(
        {
            "source_id": f"scan-{symbol}",
            "symbol": symbol,
            "observed_at": by_symbol[symbol].observed_at.isoformat()
            if symbol in by_symbol
            else None,
            "status": "AVAILABLE" if symbol in by_symbol else "UNAVAILABLE_FROM_DISCOVERY_SCAN",
            "disposition": by_symbol[symbol].disposition if symbol in by_symbol else None,
            "signal": by_symbol[symbol].signal if symbol in by_symbol else None,
            "trade_idea": by_symbol[symbol].trade_idea if symbol in by_symbol else None,
            "candidate": by_symbol[symbol].candidate if symbol in by_symbol else None,
            "risk_decision": by_symbol[symbol].risk_decision if symbol in by_symbol else None,
            "option_diagnostics": (
                by_symbol[symbol].option_diagnostics if symbol in by_symbol else None
            ),
            "reason_codes": by_symbol[symbol].reason_codes if symbol in by_symbol else (),
        }
        for symbol in symbols
    )
    input_payload = {
        "universe": symbols,
        "sources": evidence,
        "scan_run_id": str(research_run.scan_run_id),
    }

    try:
        report = await run_watchlist_research(provider, symbols=symbols, evidence=evidence)
        assert completed_run.completed_at is not None
        report = report.model_copy(update={"as_of": completed_run.completed_at})
    except (APITimeoutError, TimeoutError) as error:
        failure_reason = "AI_PROVIDER_TIMEOUT"
        await _save_watchlist_ai_run(
            request,
            context,
            provider=provider_name,
            model=model,
            input_payload=input_payload,
            output_payload={"degraded": True, "failure_reason": failure_reason},
            degraded=True,
            failure_reason=failure_reason,
        )
        logger.warning(
            "watchlist_research_provider_timeout",
            extra={
                "event": "watchlist_research_provider_timeout",
                "workspace_id": str(context.workspace_id),
                "provider": provider_name,
                "timeout_seconds": watchlist_ai_timeout,
            },
        )
        raise HTTPException(
            status_code=504,
            detail=(
                "Watchlist AI research timed out; the market scan completed "
                "but no recommendations were returned"
            ),
        ) from error
    except StructuredOutputError as error:
        failure_reason = "AI_SCHEMA_INVALID"
        await _save_watchlist_ai_run(
            request,
            context,
            provider=provider_name,
            model=model,
            input_payload=input_payload,
            output_payload={
                "degraded": True,
                "failure_reason": failure_reason,
                "diagnostics": error.diagnostics,
            },
            degraded=True,
            failure_reason=failure_reason,
        )
        logger.warning(
            "watchlist_research_schema_invalid",
            extra={
                "event": "watchlist_research_schema_invalid",
                "workspace_id": str(context.workspace_id),
                "provider": provider_name,
                "diagnostics": error.diagnostics,
                "stop_reason": error.stop_reason,
            },
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Watchlist research provider returned an invalid structured response; "
                "no watchlist changes were made. Retry the research request."
            ),
        ) from error
    except Exception as error:
        failure_reason = type(error).__name__
        await _save_watchlist_ai_run(
            request,
            context,
            provider=provider_name,
            model=model,
            input_payload=input_payload,
            output_payload={"degraded": True, "failure_reason": failure_reason},
            degraded=True,
            failure_reason=failure_reason,
        )
        logger.warning(
            "watchlist_research_unavailable",
            extra={
                "event": "watchlist_research_unavailable",
                "workspace_id": str(context.workspace_id),
                "provider": provider_name,
                "failure_type": type(error).__name__,
            },
        )
        raise HTTPException(
            status_code=422,
            detail=f"Watchlist research unavailable ({type(error).__name__})",
        ) from error

    researched_at = datetime.now(UTC)
    await _save_watchlist_ai_run(
        request,
        context,
        provider=provider_name,
        model=model,
        input_payload=input_payload,
        output_payload=report.model_dump(mode="json"),
        degraded=False,
        failure_reason=None,
    )
    return WatchlistResearchView(
        provider=provider_name,
        model=model,
        scan_run_id=research_run.scan_run_id,
        scan_completed_at=completed_run.completed_at,
        researched_at=researched_at,
        universe=symbols,
        report=report,
    )


@router.get("/opportunities", response_model=list[ConnectedAnalysis])
async def list_opportunities(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> list[ConnectedAnalysis]:
    async with request.app.state.database.sessions() as session:
        records = await session.scalars(
            select(ConnectedOpportunityRecord)
            .outerjoin(
                ConnectedScanRunRecord,
                ConnectedScanRunRecord.scan_run_id == ConnectedOpportunityRecord.scan_run_id,
            )
            .where(
                ConnectedOpportunityRecord.workspace_id == context.workspace_id,
                or_(
                    ConnectedScanRunRecord.trigger.is_(None),
                    ConnectedScanRunRecord.trigger != "AI_RESEARCH",
                ),
            )
            .order_by(ConnectedOpportunityRecord.created_at.desc())
            .limit(50)
        )
        return [ConnectedAnalysis.model_validate(record.payload) for record in records]


@router.get("/opportunities/{opportunity_id}", response_model=ConnectedAnalysis)
async def get_opportunity(
    opportunity_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> ConnectedAnalysis:
    async with request.app.state.database.sessions() as session:
        record = await session.scalar(
            select(ConnectedOpportunityRecord)
            .outerjoin(
                ConnectedScanRunRecord,
                ConnectedScanRunRecord.scan_run_id == ConnectedOpportunityRecord.scan_run_id,
            )
            .where(
                ConnectedOpportunityRecord.workspace_id == context.workspace_id,
                ConnectedOpportunityRecord.opportunity_id == opportunity_id,
                or_(
                    ConnectedScanRunRecord.trigger.is_(None),
                    ConnectedScanRunRecord.trigger != "AI_RESEARCH",
                ),
            )
        )
    if record is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return ConnectedAnalysis.model_validate(record.payload)


async def _next_session_window(
    request: Request, context: WorkspaceContext
) -> tuple[date, datetime]:
    environment = await _workspace_environment(request, context.workspace_id)
    secrets = await _credential_store(request).reveal(
        context.workspace_id, _alpaca_provider(environment)
    )
    if secrets is None:
        raise HTTPException(status_code=409, detail="Verified target Alpaca credentials required")
    adapter = AlpacaMarketClockAdapter(
        str(secrets["api_key_id"]), str(secrets["secret_key"]), environment=environment
    )
    try:
        clock = await adapter.get_clock()
    except Exception as error:
        raise HTTPException(status_code=422, detail="Market calendar unavailable") from error
    eastern = ZoneInfo("America/New_York")
    session_date = clock.next_open.astimezone(eastern).date()
    expires_at = clock.next_close + timedelta(minutes=5)
    if expires_at.astimezone(eastern).date() != session_date:
        expires_at = clock.next_open + timedelta(hours=7)
    return session_date, expires_at


@router.get("/approvals", response_model=list[ConditionalApprovalView])
async def list_conditional_approvals(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> list[ConditionalApprovalView]:
    async with request.app.state.database.sessions() as session:
        rows = list(
            await session.execute(
                select(ConditionalApprovalRecord, ConnectedOpportunityRecord.symbol)
                .outerjoin(
                    ConnectedOpportunityRecord,
                    (
                        ConnectedOpportunityRecord.workspace_id
                        == ConditionalApprovalRecord.workspace_id
                    )
                    & (
                        ConnectedOpportunityRecord.opportunity_id
                        == ConditionalApprovalRecord.opportunity_id
                    ),
                )
                .where(ConditionalApprovalRecord.workspace_id == context.workspace_id)
                .order_by(ConditionalApprovalRecord.created_at.desc())
                .limit(50)
            )
        )
    return [_conditional_approval_view(record, symbol) for record, symbol in rows]


def _validate_open_approval_renewal(
    existing: ConditionalApprovalRecord,
    intent: OrderIntent,
) -> None:
    structure_fingerprint = order_structure_fingerprint(intent)
    if existing.structure_fingerprint != structure_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="The approved open structure changed; require a new approval",
        )


@router.post(
    "/opportunities/{opportunity_id}/approve-session",
    response_model=ConditionalApprovalView,
)
async def approve_for_next_session(
    opportunity_id: UUID,
    payload: ConditionalApprovalInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> ConditionalApprovalView:
    session_date, expires_at = await _next_session_window(request, context)
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        opportunity_record, scan_trigger = (
            await session.execute(
                select(ConnectedOpportunityRecord, ConnectedScanRunRecord.trigger)
                .outerjoin(
                    ConnectedScanRunRecord,
                    ConnectedScanRunRecord.scan_run_id == ConnectedOpportunityRecord.scan_run_id,
                )
                .where(
                    ConnectedOpportunityRecord.workspace_id == context.workspace_id,
                    ConnectedOpportunityRecord.opportunity_id == opportunity_id,
                )
            )
        ).one_or_none()
        if opportunity_record is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        opportunity = ConnectedAnalysis.model_validate(opportunity_record.payload)
        workspace = await session.get(WorkspaceRecord, context.workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if opportunity_record.expires_at <= now or opportunity.expires_at <= now:
            raise HTTPException(
                status_code=409, detail="Opportunity expired; analyze the symbol again"
            )
        if scan_trigger == "AI_RESEARCH":
            raise HTTPException(status_code=409, detail="Read-only research cannot be approved")
        if opportunity.source != "ALPACA_REAL" or opportunity.disposition not in {
            "TRADE",
            "PRE_SCAN_CANDIDATE",
        }:
            raise HTTPException(
                status_code=409,
                detail="Only execution or pre-scan candidate opportunities can be approved",
            )
        if opportunity.candidate is None:
            raise HTTPException(status_code=409, detail="Opportunity has no immutable candidate")
        candidate = RankedCandidate.model_validate(opportunity.candidate)
        if opportunity.risk_decision is None:
            raise HTTPException(
                status_code=409, detail="Opportunity has no immutable risk decision"
            )
        risk_decision = RiskDecision.model_validate(opportunity.risk_decision)
        if risk_decision.decision is not RiskDecisionValue.APPROVE:
            raise HTTPException(status_code=409, detail="Opportunity risk decision is not approved")
        if opportunity.order_intent is None:
            try:
                intent = create_order_intent(
                    risk_decision, candidate, quantity=candidate.structure.quantity
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=409, detail="Opportunity cannot be approved"
                ) from error
        else:
            intent = OrderIntent.model_validate(opportunity.order_intent)
        broker_account = await session.scalar(
            select(BrokerAccountRecord).where(
                BrokerAccountRecord.workspace_id == context.workspace_id,
                BrokerAccountRecord.environment == workspace.trading_environment,
            )
        )
        if broker_account is None:
            raise HTTPException(status_code=409, detail="Target broker account is not reconciled")
        if payload.exit_plan is None:
            raise HTTPException(
                status_code=409,
                detail="An explicit exit plan is required; opening approval is not exit protection",
            )
        if payload.exit_plan.expires_at <= now:
            raise HTTPException(status_code=422, detail="Exit plan expiry must be in the future")
        live_confirmation = None
        if workspace.trading_environment == TradingEnvironment.LIVE.value:
            if payload.live_order_confirmation != LIVE_CONFIRMATION_PHRASE:
                raise HTTPException(
                    status_code=409,
                    detail="Explicit first-live-order confirmation is required",
                )
            live_confirmation = {
                "client_order_id": intent.client_order_id,
                "broker_account_id": broker_account.account_id,
                "environment": TradingEnvironment.LIVE.value,
            }
        max_limit_price = payload.max_limit_price or intent.limit_price
        max_loss = payload.max_loss or candidate.structure.max_loss
        max_quantity = payload.max_quantity or intent.quantity
        if max_limit_price < intent.limit_price:
            raise HTTPException(status_code=422, detail="Maximum price is below the approved limit")
        if max_loss < candidate.structure.max_loss:
            raise HTTPException(status_code=422, detail="Maximum loss is below the candidate risk")
        if max_quantity < intent.quantity:
            raise HTTPException(
                status_code=422, detail="Maximum quantity is below the candidate quantity"
            )
        existing = await session.scalar(
            select(ConditionalApprovalRecord)
            .where(
                ConditionalApprovalRecord.workspace_id == context.workspace_id,
                ConditionalApprovalRecord.opportunity_id == opportunity_id,
            )
            .with_for_update()
        )
        approval_action = "CONDITIONAL_APPROVAL_CREATED"
        if existing is not None:
            if approval_is_active(existing.state, existing.expires_at, now):
                raise HTTPException(
                    status_code=409, detail="Opportunity already has an active approval"
                )
            if not (
                approval_can_be_renewed(existing.state)
                or (
                    existing.state == ApprovalState.APPROVED_FOR_SESSION
                    and existing.expires_at <= now
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Opportunity already has a submitted or unresolved approval",
                )
            _validate_open_approval_renewal(existing, intent)
            record = existing
            exit_plan = payload.exit_plan.model_dump(mode="json")
            exit_plan.update(
                {
                    "opening_approval_id": str(record.approval_id),
                    "structure_fingerprint": order_structure_fingerprint(intent),
                    "broker_account_id": broker_account.account_id,
                    "environment": workspace.trading_environment,
                }
            )
            invalid_exit_plan = validate_exit_plan(exit_plan)
            if invalid_exit_plan:
                raise HTTPException(status_code=422, detail=invalid_exit_plan)
            record.approved_by_user_id = context.principal.user_id
            record.state = ApprovalState.APPROVED_FOR_SESSION
            record.approval_kind = "OPEN"
            record.session_date = session_date
            record.approved_at = now
            record.expires_at = expires_at
            record.structure_fingerprint = order_structure_fingerprint(intent)
            record.approved_intent_payload = intent.model_dump(mode="json")
            record.approved_structure_identity = candidate_structure_identity(candidate)
            record.exit_plan_payload = exit_plan
            record.approved_broker_account_id = broker_account.account_id
            record.execution_environment = workspace.trading_environment
            record.live_order_confirmation = live_confirmation
            record.max_limit_price = max_limit_price
            record.max_loss = max_loss
            record.max_quantity = max_quantity
            record.max_quote_age_seconds = payload.max_quote_age_seconds
            record.broker_order_id = None
            record.failure_reason = None
            record.claimed_at = None
            record.claim_token = None
            record.submitted_at = None
            record.updated_at = now
            approval_action = "CONDITIONAL_APPROVAL_RENEWED"
        else:
            approval_id = uuid4()
            exit_plan = payload.exit_plan.model_dump(mode="json")
            exit_plan.update(
                {
                    "opening_approval_id": str(approval_id),
                    "structure_fingerprint": order_structure_fingerprint(intent),
                    "broker_account_id": broker_account.account_id,
                    "environment": workspace.trading_environment,
                }
            )
            invalid_exit_plan = validate_exit_plan(exit_plan)
            if invalid_exit_plan:
                raise HTTPException(status_code=422, detail=invalid_exit_plan)
            record = ConditionalApprovalRecord(
                approval_id=approval_id,
                workspace_id=context.workspace_id,
                opportunity_id=opportunity_id,
                approved_by_user_id=context.principal.user_id,
                state=ApprovalState.APPROVED_FOR_SESSION,
                approval_kind="OPEN",
                session_date=session_date,
                approved_at=now,
                expires_at=expires_at,
                client_order_id=intent.client_order_id,
                structure_fingerprint=order_structure_fingerprint(intent),
                approved_intent_payload=intent.model_dump(mode="json"),
                approved_structure_identity=candidate_structure_identity(candidate),
                exit_plan_payload=exit_plan,
                approved_broker_account_id=broker_account.account_id,
                execution_environment=workspace.trading_environment,
                live_order_confirmation=live_confirmation,
                max_limit_price=max_limit_price,
                max_loss=max_loss,
                max_quantity=max_quantity,
                max_quote_age_seconds=payload.max_quote_age_seconds,
                broker_order_id=None,
                failure_reason=None,
                claimed_at=None,
                submitted_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
        try:
            await session.flush()
        except IntegrityError as error:
            raise HTTPException(
                status_code=409,
                detail="Opportunity already has an approval",
            ) from error
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                workspace_id=context.workspace_id,
                actor_user_id=context.principal.user_id,
                action=approval_action,
                detail={
                    "approval_id": str(record.approval_id),
                    "opportunity_id": str(opportunity_id),
                    "session_date": session_date.isoformat(),
                },
                occurred_at=now,
            )
        )
        return _conditional_approval_view(record, opportunity.symbol)


@router.post(
    "/broker/positions/{symbol_or_asset_id}/approve-close-session",
    response_model=ConditionalApprovalView,
)
async def approve_position_close_for_next_session(
    symbol_or_asset_id: str,
    payload: ConditionalExitApprovalInput,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> ConditionalApprovalView:
    session_date, expires_at = await _next_session_window(request, context)
    environment = await _workspace_environment(request, context.workspace_id)
    secrets = await _credential_store(request).reveal(
        context.workspace_id, _alpaca_provider(environment)
    )
    if secrets is None:
        raise HTTPException(status_code=409, detail="Alpaca credential unavailable")
    adapter = (
        AlpacaPaperBrokerAdapter(str(secrets["api_key_id"]), str(secrets["secret_key"]))
        if environment is TradingEnvironment.PAPER
        else AlpacaBrokerAdapter(
            str(secrets["api_key_id"]),
            str(secrets["secret_key"]),
            environment=environment,
        )
    )
    try:
        snapshot = await adapter.reconcile()
    except Exception as error:
        raise HTTPException(
            status_code=422, detail="Paper position could not be revalidated"
        ) from error
    finally:
        await adapter.close()

    position = next(
        (
            item
            for item in snapshot.positions
            if item.asset_id == symbol_or_asset_id or item.symbol == symbol_or_asset_id
        ),
        None,
    )
    if position is None:
        raise HTTPException(status_code=404, detail="Exact broker position not found")
    if position.asset_class != "us_option":
        raise HTTPException(
            status_code=422, detail="Conditional close approvals are limited to options"
        )
    quantity = position.quantity
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise HTTPException(
            status_code=422, detail="Broker position quantity is not a whole contract count"
        )
    if position.side not in {"long", "short"}:
        raise HTTPException(status_code=422, detail="Broker position side is not closeable")
    if position.current_price is None or position.current_price <= 0:
        raise HTTPException(
            status_code=409, detail="A current option price is required to bound the close"
        )
    order_side = ExitOrderSide.SELL if position.side == "long" else ExitOrderSide.BUY
    bound = payload.limit_price_bound or position.current_price
    expected_order_side = order_side.value
    terminal_order_statuses = {"filled", "canceled", "expired", "rejected", "replaced"}
    if any(
        order.symbol == position.symbol
        and order.side == expected_order_side
        and order.status.lower() not in terminal_order_statuses
        for order in snapshot.open_orders
    ):
        raise HTTPException(
            status_code=409, detail="This exact position already has an open broker close order"
        )
    structure_fingerprint = f"EXIT:{position.asset_id}:{position.symbol}:{position.side}:{quantity}"
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        existing = await session.scalar(
            select(ConditionalApprovalRecord)
            .where(
                ConditionalApprovalRecord.workspace_id == context.workspace_id,
                ConditionalApprovalRecord.approval_kind == "CLOSE",
                ConditionalApprovalRecord.position_asset_id == position.asset_id,
                ConditionalApprovalRecord.session_date == session_date,
            )
            .with_for_update()
        )
        if existing is not None:
            if approval_is_active(existing.state, existing.expires_at, now):
                raise HTTPException(
                    status_code=409,
                    detail="This exact position already has an active close approval",
                )
            if not (
                approval_can_be_renewed(existing.state)
                or (
                    existing.state == ApprovalState.APPROVED_FOR_SESSION
                    and existing.expires_at <= now
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This exact position already has a submitted or unresolved close approval"
                    ),
                )
            if existing.structure_fingerprint != structure_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail=("The approved close structure changed; require a new close approval"),
                )
            record = existing
            record.approved_by_user_id = context.principal.user_id
            record.approved_broker_account_id = snapshot.account.account_id
            record.execution_environment = environment.value
            record.state = ApprovalState.APPROVED_FOR_SESSION
            record.approved_at = now
            record.expires_at = expires_at
            record.structure_fingerprint = structure_fingerprint
            record.max_limit_price = bound if order_side is ExitOrderSide.BUY else Decimal("0")
            record.min_limit_price = bound if order_side is ExitOrderSide.SELL else None
            record.max_quantity = int(quantity)
            record.max_quote_age_seconds = payload.max_quote_age_seconds
            record.position_symbol = position.symbol
            record.position_side = position.side
            record.exit_order_side = order_side.value
            record.broker_order_id = None
            record.failure_reason = None
            record.claimed_at = None
            record.claim_token = None
            record.submitted_at = None
            record.updated_at = now
            approval_action = "CONDITIONAL_EXIT_APPROVAL_RENEWED"
        else:
            record = ConditionalApprovalRecord(
                approval_id=uuid4(),
                workspace_id=context.workspace_id,
                opportunity_id=None,
                approved_by_user_id=context.principal.user_id,
                approved_broker_account_id=snapshot.account.account_id,
                execution_environment=environment.value,
                state=ApprovalState.APPROVED_FOR_SESSION,
                approval_kind="CLOSE",
                session_date=session_date,
                approved_at=now,
                expires_at=expires_at,
                client_order_id=f"ad-exit-{uuid4().hex}",
                structure_fingerprint=structure_fingerprint,
                max_limit_price=bound if order_side is ExitOrderSide.BUY else Decimal("0"),
                min_limit_price=bound if order_side is ExitOrderSide.SELL else None,
                max_loss=Decimal("0"),
                max_quantity=int(quantity),
                max_quote_age_seconds=payload.max_quote_age_seconds,
                position_asset_id=position.asset_id,
                position_symbol=position.symbol,
                position_side=position.side,
                exit_order_side=order_side.value,
                broker_order_id=None,
                failure_reason=None,
                claimed_at=None,
                claim_token=None,
                submitted_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            approval_action = "CONDITIONAL_EXIT_APPROVAL_CREATED"
        try:
            await session.flush()
        except IntegrityError as error:
            raise HTTPException(
                status_code=409, detail="This exact position already has a close approval"
            ) from error
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                workspace_id=context.workspace_id,
                actor_user_id=context.principal.user_id,
                action=approval_action,
                detail={
                    "approval_id": str(record.approval_id),
                    "position_asset_id": position.asset_id,
                    "position_symbol": position.symbol,
                    "session_date": session_date.isoformat(),
                },
                occurred_at=now,
            )
        )
        return _conditional_approval_view(record, position.symbol)


def _conditional_approval_rejection_query(
    workspace_id: UUID, approval_id: UUID
) -> Select[tuple[ConditionalApprovalRecord, str]]:
    return (
        select(ConditionalApprovalRecord, ConnectedOpportunityRecord.symbol)
        .outerjoin(
            ConnectedOpportunityRecord,
            (ConnectedOpportunityRecord.workspace_id == ConditionalApprovalRecord.workspace_id)
            & (
                ConnectedOpportunityRecord.opportunity_id
                == ConditionalApprovalRecord.opportunity_id
            ),
        )
        .where(
            ConditionalApprovalRecord.workspace_id == workspace_id,
            ConditionalApprovalRecord.approval_id == approval_id,
        )
        .with_for_update(of=ConditionalApprovalRecord)
    )


@router.post("/approvals/{approval_id}/reject", response_model=ConditionalApprovalView)
async def reject_conditional_approval(
    approval_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> ConditionalApprovalView:
    now = datetime.now(UTC)
    async with request.app.state.database.sessions.begin() as session:
        row = await session.execute(
            _conditional_approval_rejection_query(context.workspace_id, approval_id)
        )
        result = row.first()
        if result is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        record, symbol = result
        if record.state != ApprovalState.APPROVED_FOR_SESSION:
            raise HTTPException(
                status_code=409,
                detail="Only an active approval can be rejected",
            )
        record.state = ApprovalState.REJECTED
        record.failure_reason = "operator_rejected"
        record.updated_at = now
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                workspace_id=context.workspace_id,
                actor_user_id=context.principal.user_id,
                action="CONDITIONAL_APPROVAL_REJECTED",
                detail={"approval_id": str(record.approval_id)},
                occurred_at=now,
            )
        )
        return _conditional_approval_view(record, symbol)


@router.post("/opportunities/{opportunity_id}/ai", response_model=AIWorkflowResult)
async def analyze_opportunity_with_ai(
    opportunity_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> AIWorkflowResult:
    async with request.app.state.database.sessions() as session:
        opportunity_record = await session.scalar(
            select(ConnectedOpportunityRecord).where(
                ConnectedOpportunityRecord.workspace_id == context.workspace_id,
                ConnectedOpportunityRecord.opportunity_id == opportunity_id,
            )
        )
    if opportunity_record is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    credential_store = _credential_store(request)
    async with request.app.state.database.sessions() as session:
        credentials = list(
            await session.scalars(
                select(WorkspaceCredentialRecord).where(
                    WorkspaceCredentialRecord.workspace_id == context.workspace_id,
                    WorkspaceCredentialRecord.provider.in_(("OPENROUTER", "ANTHROPIC")),
                    WorkspaceCredentialRecord.enabled.is_(True),
                    WorkspaceCredentialRecord.validation_status == "VERIFIED",
                )
            )
        )
    credential = next(
        (item for item in credentials if bool(item.configuration.get("active"))),
        next((item for item in credentials if item.provider == "OPENROUTER"), None),
    )
    if credential is None:
        raise HTTPException(status_code=409, detail="Verified AI provider credentials required")
    secrets = await credential_store.reveal(context.workspace_id, credential.provider)
    if secrets is None:
        raise HTTPException(status_code=409, detail="Verified AI provider credentials required")
    model = str(credential.configuration.get("model", ""))
    provider = (
        AnthropicProvider(
            str(secrets["api_key"]),
            model=model,
            timeout_seconds=request.app.state.settings.ai_timeout_seconds,
        )
        if credential.provider == "ANTHROPIC"
        else OpenRouterProvider(
            str(secrets["api_key"]),
            model=model,
            timeout_seconds=request.app.state.settings.ai_timeout_seconds,
        )
    )
    opportunity = ConnectedAnalysis.model_validate(opportunity_record.payload)
    evidence: dict[str, object] = {
        "sources": [
            {"source_id": "alpaca-signal", "evidence": opportunity.signal},
            {"source_id": "alpaca-candidate", "evidence": opportunity.candidate},
            {"source_id": "deterministic-risk", "evidence": opportunity.risk_decision},
        ],
        "source": opportunity.source,
        "observed_at": opportunity.observed_at.isoformat(),
    }
    result = await AIWorkflow(provider).run(evidence)
    await AIWorkflowStore(request.app.state.database.sessions, context.workspace_id).save(
        correlation_id=opportunity_id,
        input_payload=evidence,
        result=result,
    )
    return result


@router.post("/opportunities/{opportunity_id}/confirm")
async def confirm_paper_order(
    opportunity_id: UUID,
    payload: ConfirmPaperOrder,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> BrokerOrder:
    raise HTTPException(
        status_code=409,
        detail="Direct confirmation is disabled; review and approve the conditional order first",
    )
    async with request.app.state.database.sessions() as session:
        record = await session.scalar(
            select(ConnectedOpportunityRecord).where(
                ConnectedOpportunityRecord.workspace_id == context.workspace_id,
                ConnectedOpportunityRecord.opportunity_id == opportunity_id,
            )
        )
    if record is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    async with request.app.state.database.sessions() as policy_session:
        workspace = await policy_session.get(WorkspaceRecord, context.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    policy = AssessmentPolicy.from_payload(workspace.assessment_policy)
    opportunity = ConnectedAnalysis.model_validate(record.payload)
    if opportunity.source != "ALPACA_REAL" or opportunity.order_intent is None:
        raise HTTPException(
            status_code=409, detail="Only real, risk-approved intents can be confirmed"
        )
    if opportunity.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=409, detail="Order review expired; analyze the symbol again"
        )
    intent = OrderIntent.model_validate(opportunity.order_intent)
    if payload.client_order_id != intent.client_order_id:
        raise HTTPException(
            status_code=409, detail="Confirmation does not match immutable order intent"
        )
    assert opportunity.candidate is not None
    candidate = RankedCandidate.model_validate(opportunity.candidate)
    cipher = request.app.state.credential_cipher
    if cipher is None:
        raise HTTPException(
            status_code=503, detail="Encrypted credential storage is not configured"
        )
    try:
        return await execute_connected_paper_order(
            database=request.app.state.database,
            cipher=cipher,
            workspace_id=context.workspace_id,
            candidate=candidate,
            intent=intent,
            policy=policy,
        )
    except (RuntimeError, SubmissionUncertain) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _broker_store(request: Request, context: WorkspaceContext) -> PostgresBrokerProjectionStore:
    return PostgresBrokerProjectionStore(request.app.state.database.sessions, context.workspace_id)


@router.get("/broker/status")
async def broker_status(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> BrokerSyncStatus:
    return await _broker_store(request, context).get_status()


@router.get("/broker/account")
async def broker_account(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> BrokerAccount | None:
    return await _broker_store(request, context).get_account()


@router.get("/broker/positions")
async def broker_positions(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> tuple[BrokerPosition, ...]:
    return await _broker_store(request, context).list_positions()


@router.post("/broker/positions/{symbol_or_asset_id}/close", response_model=BrokerOrder)
async def close_position(
    symbol_or_asset_id: str,
    request: Request,
    context: WorkspaceContext = Depends(require_workspace),
) -> BrokerOrder:
    raise HTTPException(
        status_code=409,
        detail="Immediate closes are disabled; create and approve a conditional option close first",
    )
    secrets = await _credential_store(request).reveal(context.workspace_id, "ALPACA")
    if secrets is None:
        raise HTTPException(status_code=409, detail="Alpaca credential unavailable")
    adapter = AlpacaPaperBrokerAdapter(str(secrets["api_key_id"]), str(secrets["secret_key"]))
    try:
        order = await adapter.close_position(symbol_or_asset_id)
        projections = _broker_store(request, context)
        snapshot = await adapter.reconcile()
        await projections.apply_reconciliation(snapshot)
        return order
    except APIError as error:
        detail = str(error)
        if "market orders are only allowed during market hours" in detail:
            detail = (
                "Options market orders can only be executed during regular market hours "
                "(9:30 AM - 4:00 PM EDT). Please try again when the market opens."
            )
        elif "position not found" in detail:
            detail = (
                f"Position {symbol_or_asset_id} is already closed or does not exist on the broker."
            )
        raise HTTPException(status_code=400, detail=detail) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        await adapter.close()


@router.get("/broker/orders")
async def broker_orders(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> tuple[BrokerOrder, ...]:
    return await _broker_store(request, context).list_orders()


@router.get("/guardian/status", response_model=GuardianStatus)
async def guardian_status(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> GuardianStatus:
    return await PostgresGuardianStore(
        request.app.state.database.sessions, context.workspace_id
    ).status()


@router.post("/guardian/halt", response_model=GuardianStatus)
async def guardian_halt(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> GuardianStatus:
    guardian = PostgresGuardianStore(request.app.state.database.sessions, context.workspace_id)
    await guardian.halt(
        (GuardianTrigger.MANUAL_KILL_SWITCH,),
        "Operator activated the connected paper kill switch.",
    )
    return await guardian.status()


@router.post("/guardian/recover", response_model=GuardianStatus)
async def guardian_recover(
    request: Request, context: WorkspaceContext = Depends(require_workspace)
) -> GuardianStatus:
    broker = _broker_store(request, context)
    status = await broker.get_status()
    fresh = status.last_reconciled_at is not None and datetime.now(
        UTC
    ) - status.last_reconciled_at <= timedelta(seconds=90)
    known = status.state is BrokerState.RECONCILED and status.stream_connected and fresh
    guardian = PostgresGuardianStore(request.app.state.database.sessions, context.workspace_id)
    try:
        return await guardian.recover(broker_state_known=known)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
