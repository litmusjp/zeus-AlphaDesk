from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from apps.api.routes import desk as desk_routes
from packages.auth.dependencies import AuthPrincipal, WorkspaceContext
from packages.connected.market_clock import ConnectedMarketClock, SessionCalendarUnavailable

NY = ZoneInfo("America/New_York")
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000012")


def context() -> WorkspaceContext:
    return WorkspaceContext(
        principal=AuthPrincipal(
            user_id=UUID("00000000-0000-0000-0000-000000000011"),
            auth_subject="test",
            email="test@example.test",
            is_admin=False,
        ),
        workspace_id=WORKSPACE_ID,
        status="ACTIVE",
    )


class CredentialStore:
    async def reveal(self, _workspace_id: UUID, _provider: str) -> dict[str, str]:
        return {"api_key_id": "key", "secret_key": "secret"}


class ClockAdapter:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def get_clock(self) -> ConnectedMarketClock:
        return ConnectedMarketClock(
            is_open=False,
            timestamp=datetime(2026, 9, 25, 18, tzinfo=UTC),
            next_open=datetime(2026, 9, 28, 13, 30, tzinfo=UTC),
            next_close=datetime(2026, 9, 28, 20, tzinfo=UTC),
        )


class CalendarAdapter:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def get_session(self, session_date: date) -> SimpleNamespace:
        assert session_date == date(2026, 9, 28)
        return SimpleNamespace(
            open=datetime(2026, 9, 28, 9, 30, tzinfo=NY),
            close=datetime(2026, 9, 28, 16, tzinfo=NY),
        )


@pytest.mark.asyncio
async def test_next_session_exit_uses_calendar_close_minus_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def environment(*_args):
        return desk_routes.TradingEnvironment.PAPER

    monkeypatch.setattr(desk_routes, "_workspace_environment", environment)
    monkeypatch.setattr(desk_routes, "_credential_store", lambda _request: CredentialStore())
    monkeypatch.setattr(desk_routes, "AlpacaMarketClockAdapter", ClockAdapter)
    monkeypatch.setattr(desk_routes, "AlpacaSessionCalendarAdapter", CalendarAdapter)

    result = await desk_routes._next_session_exit_recommendation(object(), context())

    assert result.available is True
    assert result.session_date == date(2026, 9, 28)
    assert result.recommended_exit_at == datetime(2026, 9, 28, 15, 55, tzinfo=NY)
    assert result.recommended_exit_at < result.session_close


@pytest.mark.asyncio
async def test_next_session_exit_is_unavailable_when_calendar_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def environment(*_args):
        return desk_routes.TradingEnvironment.PAPER

    monkeypatch.setattr(desk_routes, "_workspace_environment", environment)
    monkeypatch.setattr(desk_routes, "_credential_store", lambda _request: CredentialStore())
    monkeypatch.setattr(desk_routes, "AlpacaMarketClockAdapter", ClockAdapter)

    class UnavailableCalendar(CalendarAdapter):
        async def get_session(self, _session_date: date) -> SimpleNamespace:
            raise SessionCalendarUnavailable("offline")

    monkeypatch.setattr(desk_routes, "AlpacaSessionCalendarAdapter", UnavailableCalendar)

    result = await desk_routes.next_session_exit(object(), context())

    assert result.available is False
    assert result.recommended_exit_at is None
    assert "unavailable" in (result.unavailable_reason or "").lower()
