"""Persist explicit immutable overnight exit plans on opening approvals."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0022"
down_revision: str | None = "20260920_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conditional_approvals", sa.Column("exit_plan_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conditional_approvals", "exit_plan_payload")
