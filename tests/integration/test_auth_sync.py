from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from packages.auth.dependencies import _sync_user
from packages.auth.jwt import VerifiedIdentity
from packages.database.models import AppUserRecord
from packages.database.session import Database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ALPHADESK_RUN_INTEGRATION") != "1",
        reason="Set ALPHADESK_RUN_INTEGRATION=1 to use local PostgreSQL.",
    ),
]


@pytest.mark.asyncio
async def test_concurrent_first_login_syncs_one_user() -> None:
    database = Database(
        os.getenv(
            "ALPHADESK_TEST_DATABASE_URL",
            "postgresql+psycopg://alphadesk:alphadesk_dev@localhost:5432/alphadesk",
        )
    )
    identity = VerifiedIdentity(
        subject=f"auth-sync-{uuid4()}", email=f"auth-sync-{uuid4()}@example.test"
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                database=database,
                settings=SimpleNamespace(admin_emails=frozenset()),
            )
        )
    )
    try:
        principals = await asyncio.gather(*(_sync_user(request, identity) for _ in range(8)))
        assert len({principal.user_id for principal in principals}) == 1
        assert {principal.auth_subject for principal in principals} == {identity.subject}
        async with database.sessions() as session:
            records = await session.scalars(
                select(AppUserRecord).where(AppUserRecord.auth_subject == identity.subject)
            )
            assert len(list(records)) == 1
    finally:
        async with database.sessions.begin() as session:
            await session.execute(
                delete(AppUserRecord).where(AppUserRecord.auth_subject == identity.subject)
            )
        await database.close()
