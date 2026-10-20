"""Add scoped managed assessment quality settings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261020_0024"
down_revision: str | None = "20261017_0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "managed_review_gates",
        sa.Column("quality_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "managed_review_gates",
        sa.Column("quality_revision", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("managed_review_gates", "quality_revision")
    op.drop_column("managed_review_gates", "quality_settings")
