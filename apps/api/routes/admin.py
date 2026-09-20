from __future__ import annotations

# FastAPI dependencies are intentionally declared in parameter defaults.
# ruff: noqa: B008
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from packages.auth.dependencies import AuthPrincipal, require_admin
from packages.auth.invitations import InvitationService
from packages.auth.workspaces import AdminWorkspaceProvisioningService
from packages.broker.alpaca_adapter import AlpacaBrokerAdapter
from packages.database.models import (
    AuditRecord,
    BrokerOrderRecord,
    BrokerPositionRecord,
    ConditionalApprovalRecord,
    GuardianIncidentRecord,
    InvitationRecord,
    LivePreparationRecord,
    WatchlistSymbolRecord,
    WorkspaceCredentialRecord,
    WorkspaceRecord,
)
from packages.domain.system import TradingEnvironment
from packages.execution.conditional_approval import ApprovalState
from packages.security.store import CredentialStore
from packages.trading.environment import (
    LIVE_CONFIRMATION_PHRASE,
    ModeTransitionRequest,
    TradingModeReadiness,
    validate_mode_transition,
)
from packages.trading.preparation import prepare_live_target

router = APIRouter(prefix="/admin", tags=["admin"])


class CreateInvitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    comment: str = Field(default="", max_length=240)
    max_uses: int = Field(default=1, ge=1, le=100)
    expires_in_days: int | None = Field(default=7, ge=1, le=90)


class InvitationAdminView(BaseModel):
    model_config = ConfigDict(frozen=True)

    invitation_id: UUID
    comment: str
    max_uses: int
    use_count: int
    expires_at: datetime | None
    disabled_at: datetime | None
    created_at: datetime
    invitation_code: str | None = None


class AdminWorkspaceProvisionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    status: str
    created: bool
    watchlist_count: int


class TradingEnvironmentChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: TradingEnvironment
    confirmation: str | None = None


class TradingEnvironmentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: TradingEnvironment
    live_confirmation_phrase: str
    dangerous_warning: str
    preparation_state: str | None = None
    prepared_at: datetime | None = None
    target_account_id: str | None = None


@router.get("/workspace/trading-environment", response_model=TradingEnvironmentView)
async def trading_environment(
    request: Request,
    admin: AuthPrincipal = Depends(require_admin),
) -> TradingEnvironmentView:
    async with request.app.state.database.sessions() as session:
        workspace = await session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.owner_user_id == admin.user_id)
        )
        preparation = None if workspace is None else await session.get(
            LivePreparationRecord, workspace.workspace_id
        )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return TradingEnvironmentView(
        environment=TradingEnvironment(workspace.trading_environment),
        live_confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
        dangerous_warning=(
            "LIVE sends approved orders to the live Alpaca account. Keep PAPER unless "
            "every readiness gate is green."
        ),
        preparation_state=None if preparation is None else preparation.state,
        prepared_at=None if preparation is None else preparation.prepared_at,
        target_account_id=None if preparation is None else preparation.target_account_id,
    )


@router.post("/workspace/trading-environment/prepare-live", response_model=TradingEnvironmentView)
async def prepare_live_environment(
    request: Request,
    admin: AuthPrincipal = Depends(require_admin),
) -> TradingEnvironmentView:
    """Prepare a separately verified LIVE target while leaving the workspace PAPER."""
    now = datetime.now(UTC)
    async with request.app.state.database.sessions() as session:
        workspace = await session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.owner_user_id == admin.user_id)
        )
        credential = None if workspace is None else await session.scalar(
            select(WorkspaceCredentialRecord).where(
                WorkspaceCredentialRecord.workspace_id == workspace.workspace_id,
                WorkspaceCredentialRecord.provider == "ALPACA_LIVE",
            )
        )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace.trading_environment != TradingEnvironment.PAPER.value:
        raise HTTPException(status_code=409, detail="Prepare LIVE is only available from PAPER")
    if not credential or not credential.enabled or credential.validation_status != "VERIFIED":
        raise HTTPException(status_code=409, detail="Verified ALPACA_LIVE credentials are required")
    cipher = request.app.state.credential_cipher
    if cipher is None:
        raise HTTPException(status_code=503, detail="Credential encryption is unavailable")
    secrets = CredentialStore(request.app.state.database.sessions, cipher)
    secret = await secrets.reveal(workspace.workspace_id, "ALPACA_LIVE")
    if not secret or not secret.get("api_key_id") or not secret.get("secret_key"):
        raise HTTPException(
            status_code=409, detail="Verified ALPACA_LIVE credentials are unavailable"
        )
    adapter = AlpacaBrokerAdapter(
        str(secret["api_key_id"]), str(secret["secret_key"]), environment=TradingEnvironment.LIVE
    )
    try:
        values = await prepare_live_target(
            adapter, credential_fingerprint=credential.fingerprint, now=now
        )
    except Exception as error:
        async with request.app.state.database.sessions.begin() as session:
            record = await session.get(
                LivePreparationRecord, workspace.workspace_id, with_for_update=True
            )
            values = {
                "state": "STALE",
                "target_environment": TradingEnvironment.LIVE.value,
                "target_account_id": "unknown",
                "credential_fingerprint": credential.fingerprint,
                "authenticated_at": now,
                "reconciled_at": now,
                "prepared_at": now,
                "account_active": False,
                "account_unblocked": False,
                "stream_connected": False,
                "failure_reason": type(error).__name__,
            }
            if record is None:
                session.add(LivePreparationRecord(workspace_id=workspace.workspace_id, **values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)
        raise HTTPException(
            status_code=422, detail="LIVE target preparation failed closed"
        ) from error
    finally:
        await adapter.close()
    async with request.app.state.database.sessions.begin() as session:
        record = await session.get(
            LivePreparationRecord, workspace.workspace_id, with_for_update=True
        )
        if record is None:
            session.add(LivePreparationRecord(**values, workspace_id=workspace.workspace_id))
        else:
            for key, value in values.items():
                setattr(record, key, value)
        session.add(AuditRecord(
            audit_id=uuid4(), workspace_id=workspace.workspace_id, actor_user_id=admin.user_id,
            action="LIVE_TARGET_PREPARED",
            detail={
                "environment": "LIVE",
                "account_id": values["target_account_id"],
                "stream_connected": False,
            },
            occurred_at=now,
        ))
    return TradingEnvironmentView(
        environment=TradingEnvironment.PAPER,
        live_confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
        dangerous_warning=(
            "LIVE is only prepared; no live orders are enabled. Enable LIVE requires "
            "a fresh recheck and confirmation."
        ),
        preparation_state="PREPARED",
        prepared_at=values["prepared_at"],
        target_account_id=str(values["target_account_id"]),
    )


@router.put("/workspace/trading-environment", response_model=TradingEnvironmentView)
async def change_trading_environment(
    payload: TradingEnvironmentChange,
    request: Request,
    admin: AuthPrincipal = Depends(require_admin),
) -> TradingEnvironmentView:
    now = datetime.now(UTC)
    blocked_reason: str | None = None
    async with request.app.state.database.sessions.begin() as session:
        workspace = await session.scalar(
            select(WorkspaceRecord)
            .where(WorkspaceRecord.owner_user_id == admin.user_id)
            .with_for_update()
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        old_environment = TradingEnvironment(workspace.trading_environment)
        target = payload.environment
        provider = "ALPACA_LIVE" if target is TradingEnvironment.LIVE else "ALPACA_PAPER"
        credential = await session.scalar(
            select(WorkspaceCredentialRecord).where(
                WorkspaceCredentialRecord.workspace_id == workspace.workspace_id,
                WorkspaceCredentialRecord.provider == provider,
            )
        )
        preparation = await session.get(LivePreparationRecord, workspace.workspace_id)
        guardian_active = await session.scalar(
            select(GuardianIncidentRecord).where(
                GuardianIncidentRecord.workspace_id == workspace.workspace_id,
                GuardianIncidentRecord.cleared_at.is_(None),
            )
        )
        positions = list(
            await session.scalars(
                select(BrokerPositionRecord).where(
                    BrokerPositionRecord.workspace_id == workspace.workspace_id
                )
            )
        )
        orders = list(
            await session.scalars(
                select(BrokerOrderRecord).where(
                    BrokerOrderRecord.workspace_id == workspace.workspace_id
                )
            )
        )
        approvals = list(
            await session.scalars(
                select(ConditionalApprovalRecord).where(
                    ConditionalApprovalRecord.workspace_id == workspace.workspace_id,
                    ConditionalApprovalRecord.state.not_in(
                        (
                            ApprovalState.REJECTED,
                            ApprovalState.CONDITION_FAILED,
                            ApprovalState.EXPIRED,
                        )
                    ),
                )
            )
        )
        active_order_statuses = {"new", "accepted", "pending_new", "partially_filled"}
        readiness = TradingModeReadiness(
            is_admin=admin.is_admin,
            target_credentials_verified=bool(
                credential
                and credential.enabled
                and credential.validation_status == "VERIFIED"
            ),
            target_account_authenticated_at=(
                None if preparation is None else preparation.authenticated_at
            ),
            target_account_id=(None if preparation is None else preparation.target_account_id),
            reconciled_at=(None if preparation is None else preparation.reconciled_at),
            stream_connected=bool(preparation and preparation.stream_connected),
            account_active=bool(preparation and preparation.account_active),
            account_unblocked=bool(preparation and preparation.account_unblocked),
            guardian_halted=guardian_active is not None,
            active_or_unresolved_orders=sum(
                order.status.lower() not in {
                    "filled", "canceled", "expired", "rejected", "replaced", "done_for_day"
                }
                or order.status.lower() in active_order_statuses
                for order in orders
            ),
            active_or_unresolved_approvals=len(approvals),
            ambiguous_submissions=sum(order.status.lower() == "unknown" for order in orders),
            active_submission_leases=sum(
                approval.submission_token is not None for approval in approvals
            ),
            broker_confirmed_positions=len(positions),
            target_account_matches=bool(
                preparation
                and preparation.target_environment == target.value
                and preparation.target_account_id
            ),
            legacy_identity=any(
                item.environment is None or item.identity_validated_at is None
                for item in positions + orders
            ),
            preparation_state="STALE" if preparation is None else preparation.state,
            prepared_at=None if preparation is None else preparation.prepared_at,
            prepared_environment=None if preparation is None else preparation.target_environment,
            prepared_credential_fingerprint=(
                None if preparation is None else preparation.credential_fingerprint
            ),
            current_credential_fingerprint=None if credential is None else credential.fingerprint,
        )
        decision = validate_mode_transition(
            ModeTransitionRequest(
                actor_user_id=admin.user_id,
                is_admin=admin.is_admin,
                old_environment=old_environment,
                new_environment=target,
                confirmation=payload.confirmation,
            ),
            readiness,
            now=now,
        )
        detail = {
            "old_environment": old_environment.value,
            "new_environment": target.value,
            "target_broker_account_id": readiness.target_account_id,
            "target_broker_environment": target.value,
            "confirmation_required": target is TradingEnvironment.LIVE,
            "confirmation_result": payload.confirmation == LIVE_CONFIRMATION_PHRASE,
            "reason": decision.reason,
        }
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                workspace_id=workspace.workspace_id,
                actor_user_id=admin.user_id,
                action="TRADING_ENVIRONMENT_TRANSITION_ATTEMPTED",
                detail={"allowed": decision.allowed, **detail},
                occurred_at=now,
            )
        )
        if not decision.allowed:
            blocked_reason = decision.reason
        else:
            workspace.trading_environment = target.value
            workspace.updated_at = now
            session.add(
                AuditRecord(
                    audit_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    actor_user_id=admin.user_id,
                    action="TRADING_ENVIRONMENT_CHANGED",
                    detail=detail,
                    occurred_at=now,
                )
            )
    if blocked_reason is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Trading environment change blocked: {blocked_reason}",
        )
    return TradingEnvironmentView(
        environment=payload.environment,
        live_confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
        dangerous_warning="LIVE sends approved orders to the live Alpaca account.",
    )


def _view(record: InvitationRecord, token: str | None = None) -> InvitationAdminView:
    return InvitationAdminView(
        invitation_id=record.invitation_id,
        comment=record.comment,
        max_uses=record.max_uses,
        use_count=record.use_count,
        expires_at=record.expires_at,
        disabled_at=record.disabled_at,
        created_at=record.created_at,
        invitation_code=token,
    )


@router.post("/workspace", response_model=AdminWorkspaceProvisionView)
async def provision_admin_workspace(
    request: Request,
    admin: AuthPrincipal = Depends(require_admin),
) -> AdminWorkspaceProvisionView:
    result = await AdminWorkspaceProvisioningService(
        request.app.state.database.sessions
    ).provision(admin.user_id)
    async with request.app.state.database.sessions() as session:
        watchlist_count = len(
            tuple(
                await session.scalars(
                    select(WatchlistSymbolRecord.symbol).where(
                        WatchlistSymbolRecord.workspace_id
                        == result.workspace.workspace_id
                    )
                )
            )
        )
    return AdminWorkspaceProvisionView(
        workspace_id=result.workspace.workspace_id,
        status=result.workspace.status,
        created=result.created,
        watchlist_count=watchlist_count,
    )


@router.post("/invitations", response_model=InvitationAdminView)
async def create_invitation(
    payload: CreateInvitation,
    request: Request,
    admin: AuthPrincipal = Depends(require_admin),
) -> InvitationAdminView:
    expires_at = (
        None
        if payload.expires_in_days is None
        else datetime.now(UTC) + timedelta(days=payload.expires_in_days)
    )
    record, token = await InvitationService(request.app.state.database.sessions).create(
        created_by_user_id=admin.user_id,
        comment=payload.comment,
        max_uses=payload.max_uses,
        expires_at=expires_at,
    )
    return _view(record, token)


@router.get("/invitations", response_model=list[InvitationAdminView])
async def list_invitations(
    request: Request, _: AuthPrincipal = Depends(require_admin)
) -> list[InvitationAdminView]:
    async with request.app.state.database.sessions() as session:
        records = await session.scalars(
            select(InvitationRecord).order_by(InvitationRecord.created_at.desc())
        )
        return [_view(record) for record in records]


@router.delete("/invitations/{invitation_id}", response_model=InvitationAdminView)
async def disable_invitation(
    invitation_id: UUID,
    request: Request,
    _: AuthPrincipal = Depends(require_admin),
) -> InvitationAdminView:
    async with request.app.state.database.sessions.begin() as session:
        record = await session.get(InvitationRecord, invitation_id, with_for_update=True)
        if record is None:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if record.disabled_at is None:
            record.disabled_at = datetime.now(UTC)
        return _view(record)
