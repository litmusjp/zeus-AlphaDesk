from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete

from packages.broker.projections import PostgresBrokerProjectionStore
from packages.database.models import (
    AppUserRecord,
    BrokerAccountRecord,
    BrokerOrderRecord,
    BrokerPositionRecord,
    BrokerSyncStateRecord,
    WorkspaceRecord,
)
from packages.database.session import Database
from packages.domain.broker import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPosition,
    ReconciliationSnapshot,
)
from packages.domain.system import BrokerState

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ALPHADESK_RUN_INTEGRATION") != "1",
        reason="Set ALPHADESK_RUN_INTEGRATION=1 to use local PostgreSQL.",
    ),
]

USER_ID = UUID("91000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("91000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
async def test_reconciliation_projection_is_transactional_and_rebuildable() -> None:
    database = Database(
        os.getenv(
            "ALPHADESK_TEST_DATABASE_URL",
            "postgresql+psycopg://alphadesk:alphadesk_dev@localhost:5432/alphadesk",
        )
    )
    now = datetime.now(UTC)
    async with database.sessions.begin() as session:
        session.add(
            AppUserRecord(
                user_id=USER_ID,
                auth_subject="integration-broker-user",
                email="broker-integration@example.test",
                is_admin=False,
                created_at=now,
                last_seen_at=now,
            )
        )
        session.add(
            WorkspaceRecord(
                workspace_id=WORKSPACE_ID,
                owner_user_id=USER_ID,
                name="Broker integration",
                workspace_type="CONNECTED_PAPER",
                status="ACTIVE",
                scanner_enabled=False,
                created_at=now,
                updated_at=now,
            )
        )
    store = PostgresBrokerProjectionStore(database.sessions, WORKSPACE_ID)
    snapshot = ReconciliationSnapshot(
        account=BrokerAccount(
            account_id="integration-account",
            account_number="INTEGRATION-ONLY",
            status="ACTIVE",
            currency="USD",
            equity=Decimal("100000"),
            cash=Decimal("50000"),
            buying_power=Decimal("100000"),
            options_buying_power=Decimal("50000"),
            last_equity=Decimal("100000"),
            trading_blocked=False,
            account_blocked=False,
            trade_suspended_by_user=False,
            as_of=now,
        ),
        positions=(
            BrokerPosition(
                asset_id="integration-position",
                broker_account_id="integration-account",
                environment="PAPER",
                symbol="NVDA260918C00120000",
                asset_class="us_option",
                side="long",
                quantity=Decimal("1"),
                average_entry_price=Decimal("2"),
                cost_basis=Decimal("200"),
                unrealized_pl=Decimal("0"),
                as_of=now,
            ),
        ),
        open_orders=(),
        reconciled_at=now,
    )
    try:
        await store.set_stream_connected(True)
        await store.mark_state(BrokerState.RECONCILING)
        await store.apply_reconciliation(snapshot)
        status = await store.get_status()
        assert status.state is BrokerState.RECONCILED
        assert status.stream_connected is True
        assert (await store.get_account()).account_id == "integration-account"  # type: ignore[union-attr]
        assert [item.asset_id for item in await store.list_positions()] == ["integration-position"]
    finally:
        async with database.sessions.begin() as session:
            await session.execute(delete(BrokerOrderRecord))
            await session.execute(delete(BrokerPositionRecord))
            await session.execute(delete(BrokerAccountRecord))
            await session.execute(delete(BrokerSyncStateRecord))
            await session.execute(delete(WorkspaceRecord))
            await session.execute(delete(AppUserRecord))
        await database.close()


@pytest.mark.asyncio
async def test_fresh_session_projection_evidence_matrix() -> None:
    database = Database(
        os.getenv(
            "ALPHADESK_TEST_DATABASE_URL",
            "postgresql+psycopg://alphadesk:alphadesk_dev@localhost:5432/alphadesk",
        )
    )
    now = datetime.now(UTC)
    user_id = UUID("91000000-0000-0000-0000-000000000011")
    workspace_id = UUID("91000000-0000-0000-0000-000000000012")

    def order(
        status: str,
        filled: str,
        *,
        account_id: str = "fresh-account",
        quantity: str = "2",
        leg_quantity: Decimal | None = Decimal("2"),
    ) -> BrokerOrder:
        filled_quantity = Decimal(filled)
        return BrokerOrder(
            broker_order_id=f"fresh-{status}-{filled}",
            broker_account_id=account_id,
            environment="PAPER",
            client_order_id=f"client-{status}-{filled}",
            status=status,
            asset_class="us_option",
            order_type="limit",
            order_class="mleg",
            time_in_force="day",
            quantity=Decimal(quantity),
            filled_quantity=filled_quantity,
            filled_average_price=Decimal("1.50") if filled_quantity else None,
            limit_price=Decimal("1.50"),
            created_at=now,
            legs=(
                BrokerOrderLeg(
                    broker_order_id=f"fresh-{status}-{filled}",
                    symbol="NVDA260918C00120000",
                    side="buy",
                    quantity=leg_quantity,
                    filled_quantity=filled_quantity,
                    status=status,
                ),
            ),
        )

    account = BrokerAccount(
        account_id="fresh-account",
        environment="PAPER",
        account_number="FRESH-ONLY",
        status="ACTIVE",
        currency="USD",
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        options_buying_power=Decimal("50000"),
        last_equity=Decimal("100000"),
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        as_of=now,
    )
    try:
        async with database.sessions.begin() as session:
            session.add(
                AppUserRecord(
                    user_id=user_id,
                    auth_subject="fresh-broker-user",
                    email="fresh-broker@example.test",
                    is_admin=False,
                    created_at=now,
                    last_seen_at=now,
                )
            )
            session.add(
                WorkspaceRecord(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    name="Fresh broker projection",
                    workspace_type="CONNECTED_PAPER",
                    status="ACTIVE",
                    scanner_enabled=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        for status, filled in (("done_for_day", "0"), ("done_for_day", "1"), ("done_for_day", "2")):
            await PostgresBrokerProjectionStore(
                database.sessions, workspace_id
            ).apply_reconciliation(
                ReconciliationSnapshot(
                    account=account,
                    positions=(),
                    open_orders=(order(status, filled),),
                    reconciled_at=now,
                )
            )
            fresh_store = PostgresBrokerProjectionStore(database.sessions, workspace_id)
            readback = (await fresh_store.list_orders())[0]
            assert readback.status == status
            assert readback.filled_quantity == Decimal(filled)
            assert (await fresh_store.get_status()).state is BrokerState.RECONCILED

        fresh_store = PostgresBrokerProjectionStore(database.sessions, workspace_id)
        await fresh_store.apply_reconciliation(
            ReconciliationSnapshot(
                account=account,
                positions=(),
                open_orders=(order("accepted", "1"),),
                reconciled_at=now,
            )
        )
        assert (await fresh_store.get_status()).state is BrokerState.UNKNOWN
        assert (await fresh_store.list_orders())[0].status == "done_for_day"

        await fresh_store.apply_reconciliation(
            ReconciliationSnapshot(
                account=account,
                positions=(),
                open_orders=(order("done_for_day", "0", quantity="0"),),
                reconciled_at=now,
            )
        )
        assert (await fresh_store.get_status()).state is BrokerState.UNKNOWN
        assert (await fresh_store.list_orders())[0].status == "done_for_day"

        await fresh_store.apply_reconciliation(
            ReconciliationSnapshot(
                account=account,
                positions=(),
                open_orders=(order("done_for_day", "0", leg_quantity=None),),
                reconciled_at=now,
            )
        )
        assert (await fresh_store.get_status()).state is BrokerState.UNKNOWN
        assert (await fresh_store.list_orders())[0].status == "done_for_day"

        await fresh_store.apply_reconciliation(
            ReconciliationSnapshot(
                account=account,
                positions=(),
                open_orders=(order("done_for_day", "0", account_id="wrong-account"),),
                reconciled_at=now,
            )
        )
        assert (await fresh_store.get_status()).state is BrokerState.UNKNOWN
        assert (await fresh_store.list_orders())[0].broker_account_id == "fresh-account"
    finally:
        async with database.sessions.begin() as session:
            await session.execute(delete(BrokerOrderRecord))
            await session.execute(delete(BrokerPositionRecord))
            await session.execute(delete(BrokerAccountRecord))
            await session.execute(delete(BrokerSyncStateRecord))
            await session.execute(
                delete(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id)
            )
            await session.execute(delete(AppUserRecord).where(AppUserRecord.user_id == user_id))
        await database.close()
