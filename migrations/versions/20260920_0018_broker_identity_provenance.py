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
    for table in ("broker_positions", "broker_orders"):
        op.add_column(
            table,
            sa.Column("identity_validated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(f"ix_{table}_identity_validated_at", table, ["identity_validated_at"])


def downgrade() -> None:
    """Preserve provenance columns and NULL values during downgrade.

    The identity_validated_at columns establish whether broker identity was
    freshly validated, including preserving NULL for unvalidated rows.
    Removing them would erase the fail-closed provenance signal, and
    fabricating/restoring values would be unsafe, so this downgrade is
    intentionally a documented no-op.
    """
