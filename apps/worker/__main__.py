from __future__ import annotations

import asyncio
import signal
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from packages.broker.alpaca_adapter import AlpacaBrokerAdapter
from packages.broker.projections import PostgresBrokerProjectionStore
from packages.broker.reconciliation import ReconciliationService
from packages.configuration.settings import get_settings
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.market_clock import AlpacaMarketClockAdapter
from packages.connected.opportunities import (
    ConnectedOpportunityService,
    complete_scan_run,
    start_scan_run,
)
from packages.connected.option_scan_policy import ScanMode
from packages.database.models import (
    WatchlistSymbolRecord,
    WorkspaceCredentialRecord,
    WorkspaceRecord,
)
from packages.database.session import Database
from packages.domain.system import TradingEnvironment
from packages.event_bus.client import JetStreamEventBus
from packages.execution.conditional_exit_runner import (
    process_workspace_exit_approvals,
    queue_triggered_exit_plans,
)
from packages.execution.conditional_runner import process_workspace_approvals
from packages.execution.conditional_store import ConditionalApprovalStore
from packages.observability.logging import configure_logging, get_logger
from packages.security.credentials import CredentialCipher, CredentialConfigurationError
from packages.security.store import CredentialStore

logger = get_logger(__name__)


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def _approval_expiry_supervisor(database: Database, stop: asyncio.Event) -> None:
    store = ConditionalApprovalStore(database)
    while not stop.is_set():
        try:
            now = datetime.now(UTC)
            reclaimed = await store.reclaim_all_stale_revalidating(now=now)
            expired = await store.expire_all_before(now=now)
            if reclaimed or expired:
                logger.info(
                    "conditional_approvals_lifecycle_maintained",
                    extra={
                        "event": "conditional_approvals_lifecycle_maintained",
                        "reclaimed": reclaimed,
                        "expired": expired,
                    },
                )
        except Exception:
            logger.exception(
                "conditional_approval_expiry_failed",
                extra={"event": "conditional_approval_expiry_failed"},
            )
        if await _wait_or_stop(stop, 30):
            return


async def _periodic_reconciliation(
    service: ReconciliationService, stop: asyncio.Event, interval_seconds: int
) -> None:
    while not await _wait_or_stop(stop, interval_seconds):
        try:
            await service.reconcile()
        except Exception:
            await _wait_or_stop(stop, min(interval_seconds, 10))


async def _trade_update_supervisor(service: ReconciliationService, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await service.consume_trade_updates()
        except Exception:
            if await _wait_or_stop(stop, 5):
                return
            try:
                await service.reconcile()
            except Exception:
                await _wait_or_stop(stop, 10)


async def _set_workspace_status(database: Database, workspace_id: UUID, status: str) -> None:
    async with database.sessions.begin() as session:
        workspace = await session.get(WorkspaceRecord, workspace_id, with_for_update=True)
        if workspace is not None:
            workspace.status = status


async def _workspace_runtime(
    *,
    workspace_id: UUID,
    database: Database,
    cipher: CredentialCipher,
    interval_seconds: int,
    stop: asyncio.Event,
) -> None:
    credential_store = CredentialStore(database.sessions, cipher)
    async with database.sessions() as session:
        workspace = await session.get(WorkspaceRecord, workspace_id)
    if workspace is None:
        return
    environment = TradingEnvironment(workspace.trading_environment)
    provider = "ALPACA_LIVE" if environment is TradingEnvironment.LIVE else "ALPACA_PAPER"
    secret = await credential_store.reveal(workspace_id, provider)
    if secret is None:
        return
    adapter = AlpacaBrokerAdapter(
        str(secret["api_key_id"]), str(secret["secret_key"]), environment=environment
    )
    projections = PostgresBrokerProjectionStore(database.sessions, workspace_id, environment)
    reconciliation = ReconciliationService(adapter, projections)
    local_stop = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    try:
        await reconciliation.reconcile()
        await _set_workspace_status(database, workspace_id, "ACTIVE")
        tasks = [
            asyncio.create_task(
                _periodic_reconciliation(reconciliation, local_stop, interval_seconds),
                name=f"reconcile-{workspace_id}",
            ),
            asyncio.create_task(
                _trade_update_supervisor(reconciliation, local_stop),
                name=f"trade-updates-{workspace_id}",
            ),
        ]
        logger.info(
            "workspace_broker_ready",
            extra={"event": "workspace_broker_ready", "workspace_id": str(workspace_id)},
        )
        await stop.wait()
    except asyncio.CancelledError:
        raise
    except Exception:
        await _set_workspace_status(database, workspace_id, "CONNECTION_ERROR")
        logger.exception(
            "workspace_broker_failed",
            extra={"event": "workspace_broker_failed", "workspace_id": str(workspace_id)},
        )
    finally:
        local_stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await projections.set_stream_connected(False)
        await adapter.close()


@dataclass
class ActiveRuntime:
    fingerprint: str
    stop: asyncio.Event
    task: asyncio.Task[None]


async def _discover_credentials(database: Database) -> list[WorkspaceCredentialRecord]:
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(WorkspaceCredentialRecord)
                .join(WorkspaceRecord)
                .where(
                    or_(
                        and_(
                            WorkspaceCredentialRecord.provider == "ALPACA_PAPER",
                            WorkspaceRecord.trading_environment == TradingEnvironment.PAPER.value,
                        ),
                        and_(
                            WorkspaceCredentialRecord.provider == "ALPACA_LIVE",
                            WorkspaceRecord.trading_environment == TradingEnvironment.LIVE.value,
                        ),
                    ),
                    WorkspaceCredentialRecord.validation_status == "VERIFIED",
                    WorkspaceCredentialRecord.enabled.is_(True),
                    WorkspaceRecord.status != "SUSPENDED",
                )
                .order_by(WorkspaceCredentialRecord.updated_at)
            )
        )


async def _connection_supervisor(
    database: Database,
    cipher: CredentialCipher,
    stop: asyncio.Event,
    *,
    interval_seconds: int,
    connection_limit: int,
) -> None:
    runtimes: dict[UUID, ActiveRuntime] = {}
    try:
        while not stop.is_set():
            credentials = (await _discover_credentials(database))[:connection_limit]
            wanted = {item.workspace_id: item for item in credentials}
            for workspace_id, runtime in tuple(runtimes.items()):
                record = wanted.get(workspace_id)
                if record is None or record.fingerprint != runtime.fingerprint:
                    runtime.stop.set()
                    runtime.task.cancel()
                    await asyncio.gather(runtime.task, return_exceptions=True)
                    runtimes.pop(workspace_id, None)
            for workspace_id, record in wanted.items():
                if workspace_id in runtimes:
                    continue
                runtime_stop = asyncio.Event()
                task = asyncio.create_task(
                    _workspace_runtime(
                        workspace_id=workspace_id,
                        database=database,
                        cipher=cipher,
                        interval_seconds=interval_seconds,
                        stop=runtime_stop,
                    ),
                    name=f"workspace-broker-{workspace_id}",
                )
                runtimes[workspace_id] = ActiveRuntime(record.fingerprint, runtime_stop, task)
            await _wait_or_stop(stop, 10)
    finally:
        for runtime in runtimes.values():
            runtime.stop.set()
            runtime.task.cancel()
        await asyncio.gather(*(item.task for item in runtimes.values()), return_exceptions=True)


def _market_is_open(now: datetime) -> bool:
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    minutes = eastern.hour * 60 + eastern.minute
    return eastern.weekday() < 5 and 570 <= minutes < 960


async def _alpaca_market_is_open(
    credential_store: CredentialStore,
    workspace_id: UUID,
    environment: TradingEnvironment = TradingEnvironment.PAPER,
) -> bool:
    provider = "ALPACA_LIVE" if environment is TradingEnvironment.LIVE else "ALPACA_PAPER"
    secret = await credential_store.reveal(workspace_id, provider)
    if secret is None:
        return False
    try:
        clock = await AlpacaMarketClockAdapter(
            str(secret["api_key_id"]), str(secret["secret_key"])
        ).get_clock()
    except Exception:
        logger.warning(
            "workspace_market_clock_unavailable",
            extra={
                "event": "workspace_market_clock_unavailable",
                "workspace_id": str(workspace_id),
            },
        )
        return False
    return bool(clock.is_open)


async def _pre_session_supervisor(
    database: Database,
    cipher: CredentialCipher,
    stop: asyncio.Event,
) -> None:
    last_pre_scans: dict[UUID, date] = {}
    credential_store = CredentialStore(database.sessions, cipher)
    japan = ZoneInfo("Asia/Tokyo")
    while not stop.is_set():
        now = datetime.now(UTC)
        local = now.astimezone(japan)
        if local.weekday() < 5 and 20 <= local.hour < 22:
            async with database.sessions() as session:
                workspaces = list(
                    await session.scalars(
                        select(WorkspaceRecord).where(
                            WorkspaceRecord.scanner_enabled.is_(True),
                            WorkspaceRecord.status == "ACTIVE",
                        )
                    )
                )
            for workspace in workspaces:
                if last_pre_scans.get(workspace.workspace_id) == local.date():
                    continue
                environment = TradingEnvironment(workspace.trading_environment)
                provider = (
                    "ALPACA_LIVE"
                    if environment is TradingEnvironment.LIVE
                    else "ALPACA_PAPER"
                )
                secret = await credential_store.reveal(workspace.workspace_id, provider)
                if secret is None:
                    continue
                async with database.sessions() as session:
                    symbols = list(
                        await session.scalars(
                            select(WatchlistSymbolRecord.symbol).where(
                                WatchlistSymbolRecord.workspace_id == workspace.workspace_id
                            )
                        )
                    )
                service = ConnectedOpportunityService(
                    database.sessions,
                    workspace.workspace_id,
                    str(secret["api_key_id"]),
                    str(secret["secret_key"]),
                    policy=AssessmentPolicy.from_payload(workspace.assessment_policy),
                )
                run = await start_scan_run(
                    database.sessions,
                    workspace.workspace_id,
                    trigger="PRE_SESSION",
                    attempted_count=len(symbols),
                )
                completed_count = 0
                failed_count = 0
                dispositions: Counter[str] = Counter()
                for symbol in symbols:
                    try:
                        result = await service.analyze(
                            symbol,
                            scan_run_id=run.scan_run_id,
                            mode=ScanMode.PRE_SCAN,
                        )
                        dispositions[result.disposition] += 1
                        completed_count += 1
                    except Exception as error:
                        failed_count += 1
                        logger.warning(
                            "workspace_pre_scan_symbol_unavailable",
                            extra={
                                "event": "workspace_pre_scan_symbol_unavailable",
                                "workspace_id": str(workspace.workspace_id),
                                "symbol": symbol,
                                "failure_type": type(error).__name__,
                            },
                        )
                await complete_scan_run(
                    database.sessions,
                    run.scan_run_id,
                    completed_count=completed_count,
                    failed_count=failed_count,
                )
                last_pre_scans[workspace.workspace_id] = local.date()
                logger.info(
                    "workspace_pre_scan_completed",
                    extra={
                        "event": "workspace_pre_scan_completed",
                        "workspace_id": str(workspace.workspace_id),
                        "scan_run_id": str(run.scan_run_id),
                        "trigger": run.trigger,
                        "attempted": len(symbols),
                        "completed": completed_count,
                        "failed": failed_count,
                        "dispositions": dict(dispositions),
                        "japan_date": local.date().isoformat(),
                    },
                )
        await _wait_or_stop(stop, 60)


async def _scanner_supervisor(
    database: Database,
    cipher: CredentialCipher,
    stop: asyncio.Event,
) -> None:
    last_scans: dict[UUID, datetime] = {}
    credential_store = CredentialStore(database.sessions, cipher)
    while not stop.is_set():
        now = datetime.now(UTC)
        if _market_is_open(now):
            async with database.sessions() as session:
                workspaces = list(
                    await session.scalars(
                        select(WorkspaceRecord).where(
                            WorkspaceRecord.status == "ACTIVE",
                        )
                    )
                )
            for workspace in workspaces:
                if not await _alpaca_market_is_open(credential_store, workspace.workspace_id):
                    continue
                if not workspace.scanner_enabled:
                    continue
                last = last_scans.get(workspace.workspace_id)
                if last is not None and now - last < timedelta(minutes=5):
                    continue
                environment = TradingEnvironment(workspace.trading_environment)
                provider = (
                    "ALPACA_LIVE"
                    if environment is TradingEnvironment.LIVE
                    else "ALPACA_PAPER"
                )
                secret = await credential_store.reveal(workspace.workspace_id, provider)
                if secret is None:
                    continue
                async with database.sessions() as session:
                    symbols = list(
                        await session.scalars(
                            select(WatchlistSymbolRecord.symbol).where(
                                WatchlistSymbolRecord.workspace_id == workspace.workspace_id
                            )
                        )
                    )
                service = ConnectedOpportunityService(
                    database.sessions,
                    workspace.workspace_id,
                    str(secret["api_key_id"]),
                    str(secret["secret_key"]),
                    policy=AssessmentPolicy.from_payload(workspace.assessment_policy),
                )
                run = await start_scan_run(
                    database.sessions,
                    workspace.workspace_id,
                    trigger="SCHEDULED",
                    attempted_count=len(symbols),
                )
                completed_count = 0
                failed_count = 0
                dispositions: Counter[str] = Counter()
                for symbol in symbols:
                    try:
                        result = await service.analyze(
                            symbol,
                            scan_run_id=run.scan_run_id,
                            mode=ScanMode.EXECUTION,
                        )
                        dispositions[result.disposition] += 1
                        completed_count += 1
                    except Exception as error:
                        failed_count += 1
                        logger.warning(
                            "workspace_scan_symbol_unavailable",
                            extra={
                                "event": "workspace_scan_symbol_unavailable",
                                "workspace_id": str(workspace.workspace_id),
                                "symbol": symbol,
                                "failure_type": type(error).__name__,
                            },
                        )
                await complete_scan_run(
                    database.sessions,
                    run.scan_run_id,
                    completed_count=completed_count,
                    failed_count=failed_count,
                )
                logger.info(
                    "workspace_scan_completed",
                    extra={
                        "event": "workspace_scan_completed",
                        "workspace_id": str(workspace.workspace_id),
                        "scan_run_id": str(run.scan_run_id),
                        "trigger": run.trigger,
                        "attempted": len(symbols),
                        "completed": completed_count,
                        "failed": failed_count,
                        "dispositions": dict(dispositions),
                    },
                )
                last_scans[workspace.workspace_id] = now
            for workspace in workspaces:
                if not await _alpaca_market_is_open(credential_store, workspace.workspace_id):
                    continue
                await process_workspace_approvals(
                    database=database,
                    cipher=cipher,
                    workspace_id=workspace.workspace_id,
                    now=now,
                )
                await queue_triggered_exit_plans(
                    database=database,
                    cipher=cipher,
                    workspace_id=workspace.workspace_id,
                )
                await process_workspace_exit_approvals(
                    database=database,
                    cipher=cipher,
                    workspace_id=workspace.workspace_id,
                    now=now,
                )
        await _wait_or_stop(stop, 30)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    event_bus = JetStreamEventBus(settings.nats_url, client_name="alphadesk-worker")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    expiry_task = asyncio.create_task(
        _approval_expiry_supervisor(database, stop), name="conditional-approval-expiry"
    )
    try:
        if settings.infrastructure_checks:
            await database.ping()
            await event_bus.connect()
            await event_bus.ensure_stream()
        if settings.credential_master_keys is None:
            logger.warning(
                "connected_workspaces_disabled",
                extra={"event": "connected_workspaces_disabled", "reason": "master_key_missing"},
            )
            await stop.wait()
            return
        try:
            cipher = CredentialCipher(settings.credential_master_keys.get_secret_value())
        except CredentialConfigurationError:
            logger.exception(
                "connected_workspaces_disabled",
                extra={"event": "connected_workspaces_disabled", "reason": "master_key_invalid"},
            )
            await stop.wait()
            return
        logger.info("worker_ready", extra={"event": "worker_ready", "mode": settings.mode.value})
        await asyncio.gather(
            _connection_supervisor(
                database,
                cipher,
                stop,
                interval_seconds=settings.broker_reconciliation_interval_seconds,
                connection_limit=settings.workspace_connection_limit,
            ),
            _scanner_supervisor(database, cipher, stop),
            _pre_session_supervisor(database, cipher, stop),
            expiry_task,
        )
    finally:
        expiry_task.cancel()
        await asyncio.gather(expiry_task, return_exceptions=True)
        await event_bus.close()
        await database.close()
        logger.info("worker_stopped", extra={"event": "worker_stopped"})


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
