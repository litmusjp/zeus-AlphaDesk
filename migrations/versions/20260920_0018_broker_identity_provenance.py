"""Track when broker projection identity was validated by broker evidence.

Revision ID: 20260920_0018
Revises: 20260920_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0018"
down_revision: str | None = "20260920_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("broker_positions", "broker_orders"):
        column = next(
            (
                item
                for item in inspector.get_columns(table)
                if item["name"] == "identity_validated_at"
            ),
            None,
        )
        if column is None:
            op.add_column(
                table,
                sa.Column("identity_validated_at", sa.DateTime(timezone=True), nullable=True),
            )
        elif not (
            isinstance(column["type"], sa.DateTime)
            and column["type"].timezone is True
            and column["nullable"] is True
            and column.get("default") is None
        ):
            raise ValueError(f"{table}.identity_validated_at has an incompatible schema")

        index_name = f"ix_{table}_identity_validated_at"
        index = next(
            (item for item in inspector.get_indexes(table) if item["name"] == index_name),
            None,
        )
        if index is None:
            op.create_index(index_name, table, ["identity_validated_at"])
        elif not (
            index["column_names"] == ["identity_validated_at"]
            and index["unique"] in (False, 0)
            and not any(index.get("dialect_options", {}).values())
            and not index.get("column_sorting")
        ):
            raise ValueError(f"{index_name} has an incompatible schema")


def downgrade() -> None:
    """Preserve provenance columns and NULL values during downgrade.

    The identity_validated_at columns establish whether broker identity was
    freshly validated, including preserving NULL for unvalidated rows.
    Removing them would erase the fail-closed provenance signal, and
    fabricating/restoring values would be unsafe, so this downgrade is
    intentionally a documented no-op.
    """
