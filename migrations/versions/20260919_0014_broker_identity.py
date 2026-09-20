"""Persist broker account/environment identity and scope approval IDs.

Revision ID: 20260919_0014
Revises: 20260919_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0014"
down_revision: str | None = "20260919_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_conditional_approval_client_order",
        "conditional_approvals",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_conditional_approval_workspace_client",
        "conditional_approvals",
        ["workspace_id", "client_order_id"],
    )
    op.add_column(
        "broker_accounts",
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="PAPER"),
    )
    op.alter_column("broker_accounts", "environment", server_default=None)
    for table in ("broker_positions", "broker_orders"):
        op.add_column(table, sa.Column("broker_account_id", sa.String(length=64), nullable=True))
        op.add_column(table, sa.Column("environment", sa.String(length=16), nullable=True))
        op.create_index(f"ix_{table}_broker_account_id", table, ["broker_account_id"])
        op.create_index(f"ix_{table}_environment", table, ["environment"])


def downgrade() -> None:
    for table in ("broker_positions", "broker_orders"):
        op.drop_index(f"ix_{table}_environment", table_name=table)
        op.drop_index(f"ix_{table}_broker_account_id", table_name=table)
        op.drop_column(table, "environment")
        op.drop_column(table, "broker_account_id")
    op.drop_column("broker_accounts", "environment")
    op.drop_constraint(
        "uq_conditional_approval_workspace_client",
        "conditional_approvals",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_conditional_approval_client_order",
        "conditional_approvals",
        ["client_order_id"],
    )
