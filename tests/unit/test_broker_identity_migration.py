import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from packages.broker.projections import MemoryBrokerProjectionStore
from packages.broker.reconciliation import BrokerExecutionGate
from packages.domain.broker import BrokerAccount, BrokerPosition
from packages.domain.system import BrokerState

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "20260919_0014_broker_identity.py"
)
CORRECTION_MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "20260920_0017_reset_unsafe_broker_identity.py"
)
PROVENANCE_MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "20260920_0018_broker_identity_provenance.py"
)
NOW = datetime(2026, 9, 20, 14, tzinfo=UTC)


def test_broker_identity_migration_does_not_backfill_legacy_rows() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "UPDATE broker_positions" not in source
    assert "UPDATE broker_orders" not in source
    assert "op.execute" not in source


def test_broker_identity_correction_is_offline_safe_and_workspace_scoped() -> None:
    source = CORRECTION_MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260920_0017"' in source
    assert 'down_revision: str | None = "20260920_0016"' in source
    assert "UPDATE broker_positions" not in source
    assert "UPDATE broker_orders" not in source
    assert "UPDATE broker_sync_state" in source
    assert "state = 'unknown'" in source
    assert "broker_projection_identity_provenance_unknown" in source
    assert "conditional_approvals" not in source
    assert "DELETE FROM broker_positions" not in source
    assert "DELETE FROM broker_orders" not in source


def test_broker_identity_correction_downgrade_is_safe_noop() -> None:
    source = CORRECTION_MIGRATION.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "broker_identity_correction", CORRECTION_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    migration.downgrade()

    downgrade_source = source[source.index("def downgrade()") :]
    assert "raise" not in downgrade_source
    assert "broker_account_id =" not in downgrade_source
    assert "environment =" not in downgrade_source


async def test_unknown_legacy_identity_cannot_open_execution_gate() -> None:
    store = MemoryBrokerProjectionStore()
    store.account = BrokerAccount(
        account_id="paper-1",
        environment="PAPER",
        account_number="123",
        status="ACTIVE",
        currency="USD",
        equity=100_000,
        cash=100_000,
        buying_power=100_000,
        options_buying_power=100_000,
        last_equity=100_000,
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        as_of=NOW,
    )
    store.positions["legacy-position"] = BrokerPosition(
        asset_id="legacy-position",
        broker_account_id="paper-1",
        environment="PAPER",
        symbol="AAPL",
        asset_class="us_equity",
        side="long",
        quantity=1,
        average_entry_price=100,
        cost_basis=100,
        unrealized_pl=0,
        current_price=100,
        as_of=NOW,
    )
    store.status = store.status.model_copy(
        update={
            "state": BrokerState.RECONCILED,
            "last_reconciled_at": NOW,
            "stream_connected": True,
        }
    )

    decision = await BrokerExecutionGate(store).evaluate()

    assert not decision.allowed
    assert "identity" in decision.reason


async def test_validated_fresh_evidence_can_open_execution_gate() -> None:
    store = MemoryBrokerProjectionStore()
    store.account = BrokerAccount(
        account_id="paper-1",
        environment="PAPER",
        account_number="123",
        status="ACTIVE",
        currency="USD",
        equity=100_000,
        cash=100_000,
        buying_power=100_000,
        options_buying_power=100_000,
        last_equity=100_000,
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        as_of=NOW,
    )
    store.status = store.status.model_copy(
        update={
            "state": BrokerState.RECONCILED,
            "last_reconciled_at": NOW,
            "stream_connected": True,
        }
    )

    decision = await BrokerExecutionGate(store).evaluate()

    assert decision.allowed


def test_provenance_migration_adds_nullable_timestamps_after_0017() -> None:
    source = PROVENANCE_MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260920_0018"' in source
    assert 'down_revision: str | None = "20260920_0017"' in source
    assert 'sa.Column("identity_validated_at", sa.DateTime(timezone=True), nullable=True)' in source
    assert "UPDATE broker_positions" not in source
    assert "UPDATE broker_orders" not in source


def test_provenance_migration_downgrade_preserves_columns_and_nulls() -> None:
    source = PROVENANCE_MIGRATION.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "broker_identity_provenance", PROVENANCE_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    migration.downgrade()

    downgrade_source = source[source.index("def downgrade()") :]
    assert "op.drop_column" not in downgrade_source
    assert "op.drop_index" not in downgrade_source
    assert "raise" not in downgrade_source
    assert "identity_validated_at" in downgrade_source
    assert "NULL" in downgrade_source
