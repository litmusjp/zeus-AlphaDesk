"""Quarantine legacy broker projection identity provenance.

Revision ID: 20260920_0017
Revises: 20260920_0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260920_0017"
down_revision: str | None = "20260920_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE broker_sync_state
        SET state = 'unknown',
            failure_reason = 'broker_projection_identity_provenance_unknown'
        """
    )


def downgrade() -> None:
    """Preserve the quarantine state; no identity values are fabricated."""
    pass
