"""Persist workspace-scoped candidate assessment controls.

Revision ID: 20260919_0013
Revises: 20260918_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260919_0013"
down_revision: str | None = "20260918_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "assessment_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("workspaces", "assessment_policy", server_default=None)


def downgrade() -> None:
    op.drop_column("workspaces", "assessment_policy")
