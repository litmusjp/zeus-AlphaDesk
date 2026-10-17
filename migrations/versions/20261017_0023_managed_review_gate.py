"""Persist a key/workspace scoped managed review lock."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261017_0023"
down_revision: str | None = "20260921_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "managed_review_gates",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "assessment_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_api_keys.key_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("control_key_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("assessment_key_id", name="uq_managed_review_gate_key"),
        sa.CheckConstraint("revision > 0", name="ck_managed_review_gate_revision_positive"),
    )


def downgrade() -> None:
    op.drop_table("managed_review_gates")
