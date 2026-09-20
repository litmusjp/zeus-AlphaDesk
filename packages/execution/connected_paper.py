from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from packages.broker.alpaca_adapter import AlpacaBrokerAdapter
from packages.broker.projections import PostgresBrokerProjectionStore
from packages.broker.reconciliation import BrokerExecutionGate
from packages.connected.assessment_policy import AssessmentPolicy, position_exposure
from packages.connected.market_clock import AlpacaMarketClockAdapter
from packages.database.models import ConditionalApprovalRecord, WorkspaceRecord
from packages.database.session import Database
from packages.domain.broker import BrokerOrder
from packages.domain.system import TradingEnvironment
from packages.domain.workflow import OrderIntent, RankedCandidate
from packages.execution.conditional_approval import (
    ApprovalState,
    candidate_structure_identity,
    order_structure_fingerprint,
)
from packages.execution.conditional_store import ConditionalApprovalStore
from packages.execution.engine import ExecutionBlocked, ExecutionEngine, SubmissionUncertain
from packages.execution.store import PostgresIntentStore
from packages.guardian.gate import GuardianExecutionGate
from packages.guardian.store import PostgresGuardianStore
from packages.risk.engine import RiskContext, RiskEngine
from packages.security.credentials import CredentialCipher
from packages.security.store import CredentialStore


async def execute_connected_paper_order(
    *,
    database: Database,
    cipher: CredentialCipher,
    workspace_id: UUID,
    candidate: RankedCandidate,
    intent: OrderIntent,
    approved_broker_account_id: str | None = None,
    policy: AssessmentPolicy,
    approval_id: UUID,
    claim_token: UUID,
    submission_token: UUID,
) -> BrokerOrder:
    return await execute_connected_order(
        database=database,
        cipher=cipher,
        workspace_id=workspace_id,
        candidate=candidate,
        intent=intent,
        approved_broker_account_id=approved_broker_account_id,
        policy=policy,
        approval_id=approval_id,
        claim_token=claim_token,
        submission_token=submission_token,
        environment=TradingEnvironment.PAPER,
    )


async def execute_connected_order(
    *,
    database: Database,
    cipher: CredentialCipher,
    workspace_id: UUID,
    candidate: RankedCandidate,
    intent: OrderIntent,
    approved_broker_account_id: str | None = None,
    policy: AssessmentPolicy,
    approval_id: UUID,
    claim_token: UUID,
    submission_token: UUID,
    environment: TradingEnvironment,
) -> BrokerOrder:
    try:
        return await _execute_connected_order(
            database=database,
            cipher=cipher,
            workspace_id=workspace_id,
            candidate=candidate,
            intent=intent,
            approved_broker_account_id=approved_broker_account_id,
            policy=policy,
            approval_id=approval_id,
            claim_token=claim_token,
            submission_token=submission_token,
            environment=environment,
        )
    except (ExecutionBlocked, SubmissionUncertain):
        raise
    except Exception as error:
        raise ExecutionBlocked("pre-submit execution preparation failed") from error


async def _execute_connected_order(
    *,
    database: Database,
    cipher: CredentialCipher,
    workspace_id: UUID,
    candidate: RankedCandidate,
    intent: OrderIntent,
    approved_broker_account_id: str | None = None,
    policy: AssessmentPolicy,
    approval_id: UUID,
    claim_token: UUID,
    submission_token: UUID,
    environment: TradingEnvironment,
) -> BrokerOrder:
    async with database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, workspace_id)
        approval = await session.scalar(
            select(ConditionalApprovalRecord).where(
                ConditionalApprovalRecord.workspace_id == workspace_id,
                ConditionalApprovalRecord.approval_id == approval_id,
                ConditionalApprovalRecord.claim_token == claim_token,
            )
        )
        other_pending_approvals = list(
            await session.scalars(
                select(ConditionalApprovalRecord).where(
                    ConditionalApprovalRecord.workspace_id == workspace_id,
                    ConditionalApprovalRecord.approval_kind == "OPEN",
                    ConditionalApprovalRecord.approval_id != approval_id,
                    ConditionalApprovalRecord.state.in_(
                        (
                            ApprovalState.REVALIDATING,
                            ApprovalState.READY_TO_SUBMIT,
                            ApprovalState.SUBMITTING,
                            ApprovalState.SUBMISSION_UNCERTAIN,
                        )
                    ),
                )
            )
        )
    if (
        workspace is None
        or approval is None
        or approval.approval_kind != "OPEN"
        or approval.execution_environment != environment.value
        or other_pending_approvals
        or approval.state != ApprovalState.SUBMITTING
        or approval.approved_broker_account_id is None
        or approved_broker_account_id != approval.approved_broker_account_id
        or intent.client_order_id != approval.client_order_id
        or order_structure_fingerprint(intent) != approval.structure_fingerprint
        or approval.approved_structure_identity != candidate_structure_identity(candidate)
        or approval.approved_intent_payload != intent.model_dump(mode="json")
        or intent.quantity > approval.max_quantity
        or intent.limit_price > approval.max_limit_price
        or candidate.structure.max_loss > approval.max_loss
        or workspace.updated_at > approval.approved_at
        or workspace.trading_environment != environment.value
        or (
            environment is TradingEnvironment.LIVE
            and approval.live_order_confirmation
            != {
                "client_order_id": intent.client_order_id,
                "broker_account_id": approved_broker_account_id,
                "environment": TradingEnvironment.LIVE.value,
            }
        )
    ):
        raise ExecutionBlocked("Approval is no longer valid for submission")
    policy = AssessmentPolicy.from_payload(workspace.assessment_policy)
    projections = PostgresBrokerProjectionStore(database.sessions, workspace_id, environment)
    account = await projections.get_account()
    if account is None:
        raise ExecutionBlocked("Broker account projection unavailable")
    if account.environment != environment.value:
        raise ExecutionBlocked("Broker account environment does not match workspace")
    if approved_broker_account_id is not None and account.account_id != approved_broker_account_id:
        raise ExecutionBlocked("Broker account changed since approval")
    if (
        account.status.upper() != "ACTIVE"
        or account.trading_blocked
        or account.account_blocked
        or account.trade_suspended_by_user
    ):
        raise ExecutionBlocked("Broker account unavailable")
    positions = await projections.list_positions()
    orders = await projections.list_orders()
    if any(
        position.identity_validated_at is None
        or position.broker_account_id != account.account_id
        or position.environment != environment.value
        for position in positions
    ):
        raise ExecutionBlocked("Broker position identity is unavailable or mismatched")
    if any(
        order.identity_validated_at is None
        or order.broker_account_id != account.account_id
        or order.environment != environment.value
        for order in orders
    ):
        raise ExecutionBlocked("Broker order identity is unavailable or mismatched")
    active_order_statuses = {"new", "accepted", "pending_new", "partially_filled"}
    terminal_order_statuses = {
        "filled",
        "canceled",
        "expired",
        "rejected",
        "replaced",
        "done_for_day",
    }
    known_asset_classes = {"us_option", "us_equity"}
    if any(
        order.asset_class.lower() not in known_asset_classes
        or order.status.lower() not in active_order_statuses | terminal_order_statuses
        for order in orders
    ):
        raise ExecutionBlocked("Broker order exposure contains an unknown state")
    if any(order.status.lower() in active_order_statuses for order in orders):
        raise ExecutionBlocked("Pending broker order exposure is unavailable for aggregate risk")
    sizing_loss = (
        candidate.structure.max_loss / Decimal(candidate.structure.quantity) * intent.quantity
    )
    sizing_error = policy.order_sizing_error(quantity=intent.quantity, max_loss=sizing_loss)
    if sizing_error is not None:
        raise ExecutionBlocked(sizing_error)
    underlying_symbol = candidate.structure.legs[0].contract.underlying_symbol
    open_planned_loss, underlying_open_risk, portfolio_greeks_available = position_exposure(
        positions, underlying_symbol, account.equity
    )
    broker_gate = BrokerExecutionGate(projections, environment=environment)
    gate = await broker_gate.evaluate()
    rerisk = RiskEngine(policy.as_risk_policy()).evaluate(
        candidate,
        RiskContext(
            paper_equity=account.equity,
            open_planned_loss=open_planned_loss,
            underlying_open_risk=underlying_open_risk,
            daily_loss=max(account.last_equity - account.equity, Decimal("0")),
            drawdown_percent=max(account.last_equity - account.equity, Decimal("0"))
            / max(account.last_equity, Decimal("1"))
            * Decimal("100"),
            concurrent_option_structures=sum(
                1 for position in positions if position.asset_class.lower() == "us_option"
            ),
            broker_execution_allowed=gate.allowed,
            portfolio_greeks_available=portfolio_greeks_available,
        ),
    )
    if rerisk.decision != "APPROVE":
        raise ExecutionBlocked("Deterministic risk no longer approves this order")
    provider = "ALPACA_LIVE" if environment is TradingEnvironment.LIVE else "ALPACA_PAPER"
    secrets = await CredentialStore(database.sessions, cipher).reveal(workspace_id, provider)
    if secrets is None:
        raise ExecutionBlocked("Alpaca credential unavailable")
    api_key = str(secrets["api_key_id"])
    secret_key = str(secrets["secret_key"])
    adapter = AlpacaBrokerAdapter(api_key, secret_key, environment=environment)
    try:
        authenticated_account = await adapter.get_account()
    except Exception as error:
        raise ExecutionBlocked("Authenticated target account unavailable") from error
    if (
        authenticated_account.account_id != account.account_id
        or (
            approved_broker_account_id is not None
            and authenticated_account.account_id != approved_broker_account_id
        )
        or authenticated_account.status.upper() != "ACTIVE"
        or authenticated_account.trading_blocked
        or authenticated_account.account_blocked
        or authenticated_account.trade_suspended_by_user
    ):
        raise ExecutionBlocked("Authenticated paper account changed or unavailable")
    try:
        market_clock = await AlpacaMarketClockAdapter(
            api_key, secret_key, environment=environment
        ).get_clock()
    except Exception as error:
        raise ExecutionBlocked("Authoritative target market clock unavailable") from error
    if not market_clock.is_open:
        raise ExecutionBlocked("Market session is closed")
    guardian = PostgresGuardianStore(database.sessions, workspace_id)
    engine = ExecutionEngine(
        adapter,
        PostgresIntentStore(database.sessions, workspace_id),
        preflight=ConnectedPreflight(broker_gate, GuardianExecutionGate(guardian)),
        submission_fence=ApprovalSubmissionFence(
            database=database,
            workspace_id=workspace_id,
            approval_id=approval_id,
            submission_token=submission_token,
        ),
        environment=environment,
    )
    order: BrokerOrder | None = None
    try:
        order = await engine.execute(
            intent,
            expected_broker_account_id=account.account_id,
        )
    except SubmissionUncertain:
        raise
    finally:
        try:
            await adapter.close()
        except Exception as error:
            if order is not None:
                raise SubmissionUncertain("post-submit adapter cleanup uncertain") from error
    if order is None:
        raise ExecutionBlocked("Order execution returned no broker order")
    return order


class ConnectedPreflight:
    def __init__(self, broker: BrokerExecutionGate, guardian: GuardianExecutionGate) -> None:
        self._broker = broker
        self._guardian = guardian

    async def execution_allowed(self) -> tuple[bool, str]:
        guardian_allowed, guardian_reason = await self._guardian.execution_allowed()
        if not guardian_allowed:
            return False, guardian_reason
        broker = await self._broker.evaluate()
        return broker.allowed, broker.reason


class ApprovalSubmissionFence:
    def __init__(
        self,
        *,
        database: Database,
        workspace_id: UUID,
        approval_id: UUID,
        submission_token: UUID,
    ) -> None:
        self._database = database
        self._workspace_id = workspace_id
        self._approval_id = approval_id
        self._submission_token = submission_token

    async def submission_allowed(self) -> tuple[bool, str]:
        async with self._database.sessions() as session:
            approval = await session.scalar(
                select(ConditionalApprovalRecord).where(
                    ConditionalApprovalRecord.workspace_id == self._workspace_id,
                    ConditionalApprovalRecord.approval_id == self._approval_id,
                    ConditionalApprovalRecord.submission_token == self._submission_token,
                )
            )
        if approval is None or approval.state != ApprovalState.SUBMITTING:
            return False, "approval ownership changed"
        return True, ""

    async def authorize_final_submission(self) -> tuple[bool, str]:
        authorized = await ConditionalApprovalStore(self._database).authorize_final_submission(
            self._approval_id,
            workspace_id=self._workspace_id,
            submission_token=self._submission_token,
        )
        return (True, "") if authorized else (False, "approval ownership changed")
