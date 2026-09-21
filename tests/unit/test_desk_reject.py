from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from apps.api.routes.desk import _conditional_approval_rejection_query
from packages.database.models import ConditionalApprovalRecord


def test_reject_query_locks_only_the_approval_row() -> None:
    query_builder = cast(
        Callable[[UUID, UUID], Select[tuple[ConditionalApprovalRecord, str]]],
        _conditional_approval_rejection_query,
    )
    query = query_builder(
        UUID("96000000-0000-0000-0000-000000000001"),
        UUID("96000000-0000-0000-0000-000000000002"),
    )

    dialect_factory = cast(Callable[[], Any], postgresql.dialect)
    sql = str(cast(Any, query).compile(dialect=dialect_factory()))

    assert "FOR UPDATE OF conditional_approvals" in sql
    assert "FOR UPDATE\n" not in sql
    assert "conditional_approvals.workspace_id" in sql
    assert "conditional_approvals.approval_id" in sql
