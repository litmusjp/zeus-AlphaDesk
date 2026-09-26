from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from packages.connected.market_clock import (
    AlpacaSessionCalendarAdapter,
    ExitOutsideSession,
    SessionCalendarUnavailable,
    SessionDateUnavailable,
)

NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")


class FakeCalendarClient:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.requests = []

    def get_calendar(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.rows


def adapter(client: FakeCalendarClient) -> AlpacaSessionCalendarAdapter:
    return AlpacaSessionCalendarAdapter("key", "secret", client=client)


def calendar_row(session_date: date, opening: datetime, closing: datetime):
    return SimpleNamespace(date=session_date, open=opening, close=closing)


@pytest.mark.asyncio
async def test_japan_local_saturday_maps_to_valid_friday_exchange_session() -> None:
    session_date = date(2026, 9, 25)
    client = FakeCalendarClient(
        [
            calendar_row(
                session_date,
                datetime(2026, 9, 25, 9, 30, tzinfo=NY),
                datetime(2026, 9, 25, 16, tzinfo=NY),
            )
        ]
    )

    result = await adapter(client).validate_exit_in_session(
        datetime(2026, 9, 26, 1, 30, tzinfo=TOKYO)
    )

    assert result == session_date
    assert client.requests[0].start == session_date
    assert client.requests[0].end == session_date


@pytest.mark.asyncio
async def test_real_exchange_saturday_is_rejected() -> None:
    client = FakeCalendarClient()

    with pytest.raises(SessionDateUnavailable):
        await adapter(client).validate_exit_in_session(datetime(2026, 9, 26, 12, tzinfo=NY))


@pytest.mark.asyncio
async def test_holiday_without_calendar_row_is_rejected() -> None:
    client = FakeCalendarClient(
        [
            calendar_row(
                date(2026, 9, 8),
                datetime(2026, 9, 8, 9, 30, tzinfo=NY),
                datetime(2026, 9, 8, 16, tzinfo=NY),
            )
        ]
    )

    with pytest.raises(SessionDateUnavailable):
        await adapter(client).validate_exit_in_session(datetime(2026, 9, 7, 12, tzinfo=NY))


@pytest.mark.asyncio
async def test_early_close_is_taken_from_calendar_response() -> None:
    session_date = date(2026, 11, 27)
    client = FakeCalendarClient(
        [
            calendar_row(
                session_date,
                datetime(2026, 11, 27, 9, 30, tzinfo=NY),
                datetime(2026, 11, 27, 13, tzinfo=NY),
            )
        ]
    )

    assert (
        await adapter(client).validate_exit_in_session(datetime(2026, 11, 27, 13, tzinfo=NY))
        == session_date
    )
    with pytest.raises(ExitOutsideSession):
        await adapter(client).validate_exit_in_session(datetime(2026, 11, 27, 13, 1, tzinfo=NY))


@pytest.mark.asyncio
async def test_dst_uses_returned_open_and_close_instants() -> None:
    session_date = date(2026, 3, 9)
    client = FakeCalendarClient(
        [
            calendar_row(
                session_date,
                datetime(2026, 3, 9, 9, 30, tzinfo=NY),
                datetime(2026, 3, 9, 16, tzinfo=NY),
            )
        ]
    )

    assert (
        await adapter(client).validate_exit_in_session(datetime(2026, 3, 9, 22, 30, tzinfo=TOKYO))
        == session_date
    )


@pytest.mark.asyncio
async def test_calendar_unavailable_fails_closed_distinctly() -> None:
    client = FakeCalendarClient(error=TimeoutError("offline"))

    with pytest.raises(SessionCalendarUnavailable, match="calendar read failed"):
        await adapter(client).validate_exit_in_session(datetime(2026, 9, 25, 12, tzinfo=NY))
