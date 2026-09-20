"""Persist the non-reassignable provider dispatch boundary.

Revision ID: 20260920_0016
Revises: 20260919_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0016"
down_revision: str | None = "20260919_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conditional_approvals",
        sa.Column("dispatch_authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_conditional_approvals_dispatch_authorized_at",
        "conditional_approvals",
        ["dispatch_authorized_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conditional_approvals_dispatch_authorized_at",
        table_name="conditional_approvals",
    )
    op.drop_column("conditional_approvals", "dispatch_authorized_at")
