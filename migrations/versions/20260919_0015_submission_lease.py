"""Add a distinct durable submission lease to conditional approvals.

Revision ID: 20260919_0015
Revises: 20260919_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0015"
down_revision: str | None = "20260919_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conditional_approvals",
        sa.Column("submission_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conditional_approvals",
        sa.Column("submission_token", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_conditional_approvals_submission_claimed_at",
        "conditional_approvals",
        ["submission_claimed_at"],
    )
    op.create_index(
        "ix_conditional_approvals_submission_token",
        "conditional_approvals",
        ["submission_token"],
    )


def downgrade() -> None:
    op.drop_index("ix_conditional_approvals_submission_token", table_name="conditional_approvals")
    op.drop_index(
        "ix_conditional_approvals_submission_claimed_at", table_name="conditional_approvals"
    )
    op.drop_column("conditional_approvals", "submission_token")
    op.drop_column("conditional_approvals", "submission_claimed_at")
