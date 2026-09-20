from pathlib import Path

from sqlalchemy import UniqueConstraint

from packages.database.models import AgentAPIKeyRecord

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "20260920_0019_agent_api_keys.py"
)


def test_agent_api_key_migration_has_global_hash_uniqueness_and_workspace_safety() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'sa.UniqueConstraint("key_hash")' in source
    assert "uq_agent_api_key_workspace_hash" not in source
    assert 'sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"]' in source
    assert (
        'for column in ("workspace_id", "key_hash", "created_at", "last_used_at", "revoked_at")'
        in source
    )

    assert AgentAPIKeyRecord.__table__.c.key_hash.unique is True
    assert all(
        "workspace_id" not in {column.name for column in constraint.columns}
        for constraint in AgentAPIKeyRecord.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    )
