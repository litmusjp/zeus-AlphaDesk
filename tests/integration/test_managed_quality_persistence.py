from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from packages.database.models import (
    AgentAPIKeyRecord,
    AppUserRecord,
    ManagedReviewGateRecord,
    WorkspaceRecord,
)
from packages.security.agent_keys import hash_agent_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ALPHADESK_RUN_INTEGRATION") != "1",
        reason="Set ALPHADESK_RUN_INTEGRATION=1 to use local PostgreSQL.",
    ),
]


def test_managed_quality_settings_survive_database_reopen() -> None:
    database_url = os.getenv(
        "ALPHADESK_TEST_DATABASE_URL",
        "postgresql+psycopg://alphadesk:alphadesk_dev@localhost:5432/alphadesk",
    )
    engine = create_engine(database_url, connect_args={"connect_timeout": 3})
    reopened = None
    user_id, workspace_id, key_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                AppUserRecord(
                    user_id=user_id,
                    auth_subject=f"managed-quality-{user_id}",
                    email=f"managed-quality-{user_id}@example.test",
                    is_admin=False,
                    created_at=now,
                    last_seen_at=now,
                )
            )
            session.flush()
            session.add(
                WorkspaceRecord(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    name="Managed quality persistence integration",
                    workspace_type="CONNECTED_PAPER",
                    status="ACTIVE",
                    scanner_enabled=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                AgentAPIKeyRecord(
                    key_id=key_id,
                    workspace_id=workspace_id,
                    key_hash=hash_agent_key(f"integration-key-{key_id}"),
                    key_prefix="integration",
                    name="managed quality persistence",
                    created_at=now,
                    last_used_at=None,
                    revoked_at=None,
                )
            )
            session.flush()
            session.add(
                ManagedReviewGateRecord(
                    workspace_id=workspace_id,
                    assessment_key_id=key_id,
                    enabled=False,
                    revision=1,
                    quality_settings={
                        "minimum_signal_score": "25",
                        "minimum_catalyst_confidence": "0.15",
                        "require_price_action_confirmation": False,
                    },
                    quality_revision=2,
                    control_key_hash=hash_agent_key(f"control-key-{key_id}"),
                    created_at=now,
                    updated_at=now,
                )
            )

        engine.dispose()
        reopened = create_engine(database_url, connect_args={"connect_timeout": 3})
        with Session(reopened) as session:
            row = session.get(ManagedReviewGateRecord, (workspace_id, key_id))
        assert row is not None
        assert row.quality_revision == 2
        assert row.quality_settings == {
            "minimum_signal_score": "25",
            "minimum_catalyst_confidence": "0.15",
            "require_price_action_confirmation": False,
        }
    finally:
        cleanup_engine = reopened or engine
        with Session(cleanup_engine) as session, session.begin():
            session.execute(
                delete(ManagedReviewGateRecord).where(
                    ManagedReviewGateRecord.workspace_id == workspace_id
                )
            )
            session.execute(delete(AgentAPIKeyRecord).where(AgentAPIKeyRecord.key_id == key_id))
            session.execute(
                delete(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id)
            )
            session.execute(delete(AppUserRecord).where(AppUserRecord.user_id == user_id))
        cleanup_engine.dispose()
