"""Add explicit two-phase LIVE preparation evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0021"
down_revision: str | None = "20260920_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_preparations",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("target_environment", sa.String(16), nullable=False),
        sa.Column("target_account_id", sa.String(64), nullable=False),
        sa.Column("credential_fingerprint", sa.String(16), nullable=False),
        sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_active", sa.Boolean(), nullable=False),
        sa.Column("account_unblocked", sa.Boolean(), nullable=False),
        sa.Column("stream_connected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_index("ix_live_preparations_state", "live_preparations", ["state"])
    op.create_index("ix_live_preparations_prepared_at", "live_preparations", ["prepared_at"])


def downgrade() -> None:
    op.drop_index("ix_live_preparations_prepared_at", table_name="live_preparations")
    op.drop_index("ix_live_preparations_state", table_name="live_preparations")
    op.drop_table("live_preparations")
