"""Persist explicit workspace trading environments and approval bindings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0020"
down_revision: str | None = "20260920_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("trading_environment", sa.String(16), nullable=False, server_default="PAPER"),
    )
    op.create_index("ix_workspaces_trading_environment", "workspaces", ["trading_environment"])
    op.add_column(
        "conditional_approvals",
        sa.Column("execution_environment", sa.String(16), nullable=False, server_default="PAPER"),
    )
    op.create_index(
        "ix_conditional_approvals_execution_environment",
        "conditional_approvals",
        ["execution_environment"],
    )
    op.add_column(
        "conditional_approvals",
        sa.Column("live_order_confirmation", sa.JSON(), nullable=True),
    )
    op.alter_column("workspaces", "trading_environment", server_default=None)
    op.alter_column("conditional_approvals", "execution_environment", server_default=None)


def downgrade() -> None:
    op.drop_column("conditional_approvals", "live_order_confirmation")
    op.drop_index(
        "ix_conditional_approvals_execution_environment", table_name="conditional_approvals"
    )
    op.drop_column("conditional_approvals", "execution_environment")
    op.drop_index("ix_workspaces_trading_environment", table_name="workspaces")
    op.drop_column("workspaces", "trading_environment")
