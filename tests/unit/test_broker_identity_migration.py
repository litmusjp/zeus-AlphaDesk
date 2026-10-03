import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

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

    decision = await BrokerExecutionGate(store).evaluate(now=NOW)

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

    decision = await BrokerExecutionGate(store).evaluate(now=NOW)

    assert decision.allowed


def test_provenance_migration_adds_nullable_timestamps_after_0017() -> None:
    source = PROVENANCE_MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260920_0018"' in source
    assert 'down_revision: str | None = "20260920_0017"' in source
    assert 'sa.Column("identity_validated_at", sa.DateTime(timezone=True), nullable=True)' in source
    assert "UPDATE broker_positions" not in source
    assert "UPDATE broker_orders" not in source


def _provenance_migration():
    spec = importlib.util.spec_from_file_location(
        "broker_identity_provenance", PROVENANCE_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_provenance_migration_downgrade_preserves_null_and_timestamp() -> None:
    migration = _provenance_migration()

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for table in ("broker_positions", "broker_orders"):
            connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))

        operations = Operations(MigrationContext.configure(connection))
        with patch.object(migration, "op", operations):
            migration.upgrade()
            for table in ("broker_positions", "broker_orders"):
                connection.execute(
                    sa.text(f"INSERT INTO {table} (id, identity_validated_at) VALUES (1, NULL)")
                )
                connection.execute(
                    sa.text(
                        f"INSERT INTO {table} (id, identity_validated_at) "
                        "VALUES (2, '2026-09-20 14:00:00')"
                    )
                )

            migration.downgrade()
            for table in ("broker_positions", "broker_orders"):
                inspector = sa.inspect(connection)
                assert "identity_validated_at" in {
                    column["name"] for column in inspector.get_columns(table)
                }
                assert f"ix_{table}_identity_validated_at" in {
                    index["name"] for index in inspector.get_indexes(table)
                }
                assert connection.execute(
                    sa.text(f"SELECT identity_validated_at FROM {table} ORDER BY id")
                ).scalars().all() == [None, "2026-09-20 14:00:00"]


def test_provenance_migration_reentrant_and_repairs_missing_index() -> None:
    migration = _provenance_migration()
    inspector = Mock()
    inspector.get_columns.return_value = [
        {"name": "identity_validated_at", "type": sa.DateTime(timezone=True), "nullable": True}
    ]
    inspector.get_indexes.return_value = [
        {
            "name": "ix_broker_positions_identity_validated_at",
            "column_names": ["identity_validated_at"],
            "unique": False,
            "dialect_options": {"postgresql_include": []},
        }
    ]
    operation = Mock()
    with (
        patch.object(migration, "op", operation),
        patch.object(migration.sa, "inspect", return_value=inspector),
    ):
        migration.upgrade()

    operation.add_column.assert_not_called()
    operation.create_index.assert_called_once_with(
        "ix_broker_orders_identity_validated_at", "broker_orders", ["identity_validated_at"]
    )


def test_provenance_migration_reupgrade_is_noop() -> None:
    migration = _provenance_migration()
    inspector = Mock()
    inspector.get_columns.return_value = [
        {"name": "identity_validated_at", "type": sa.DateTime(timezone=True), "nullable": True}
    ]
    inspector.get_indexes.side_effect = lambda table: [
        {
            "name": f"ix_{table}_identity_validated_at",
            "column_names": ["identity_validated_at"],
            "unique": False,
            "dialect_options": {"postgresql_include": []},
        }
    ]
    operation = Mock()
    with (
        patch.object(migration, "op", operation),
        patch.object(migration.sa, "inspect", return_value=inspector),
    ):
        migration.upgrade()

    operation.add_column.assert_not_called()
    operation.create_index.assert_not_called()


@pytest.mark.parametrize(
    "column",
    [
        {"type": sa.DateTime(timezone=False), "nullable": True},
        {"type": sa.Integer(), "nullable": True},
        {"type": sa.DateTime(timezone=True), "nullable": False},
        {"type": sa.DateTime(timezone=True), "nullable": True, "default": "now()"},
    ],
)
def test_provenance_migration_rejects_incompatible_column(column: dict) -> None:
    migration = _provenance_migration()
    inspector = Mock()
    inspector.get_columns.return_value = [{"name": "identity_validated_at", **column}]
    operation = Mock()
    with (
        patch.object(migration, "op", operation),
        patch.object(migration.sa, "inspect", return_value=inspector),
    ):
        with pytest.raises(ValueError, match=r"broker_positions\.identity_validated_at"):
            migration.upgrade()

    operation.add_column.assert_not_called()
    operation.create_index.assert_not_called()


@pytest.mark.parametrize(
    "index",
    [
        {"column_names": ["id"], "unique": False},
        {"column_names": ["identity_validated_at"], "unique": True},
        {
            "column_names": ["identity_validated_at"],
            "unique": False,
            "dialect_options": {"postgresql_where": "id > 0"},
        },
        {
            "column_names": ["identity_validated_at"],
            "unique": False,
            "column_sorting": {"identity_validated_at": ("desc",)},
        },
    ],
)
def test_provenance_migration_rejects_incompatible_index(index: dict) -> None:
    migration = _provenance_migration()
    inspector = Mock()
    inspector.get_columns.return_value = [
        {"name": "identity_validated_at", "type": sa.DateTime(timezone=True), "nullable": True}
    ]
    inspector.get_indexes.return_value = [
        {"name": "ix_broker_positions_identity_validated_at", **index}
    ]
    operation = Mock()
    with (
        patch.object(migration, "op", operation),
        patch.object(migration.sa, "inspect", return_value=inspector),
    ):
        with pytest.raises(ValueError, match="ix_broker_positions_identity_validated_at"):
            migration.upgrade()

    operation.add_column.assert_not_called()
    operation.create_index.assert_not_called()
