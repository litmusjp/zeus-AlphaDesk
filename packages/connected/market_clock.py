from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from pydantic import BaseModel, ConfigDict

from packages.domain.system import TradingEnvironment


class ConnectedMarketClock(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_open: bool
    timestamp: datetime
    next_open: datetime
    next_close: datetime
    timezone: str = "America/New_York"
    regular_session: str = "9:30 AM-4:00 PM ET"
    source: str = "ALPACA_REAL"


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class AlpacaMarketClockAdapter:
    """Read-only tenant-bound Alpaca market clock boundary."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        environment: TradingEnvironment = TradingEnvironment.PAPER,
        client: Any | None = None,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("Alpaca credentials are required for the market clock")
        self._client = client or TradingClient(
            api_key, secret_key, paper=environment is TradingEnvironment.PAPER
        )

    async def get_clock(self) -> ConnectedMarketClock:
        raw = await asyncio.to_thread(self._client.get_clock)
        return ConnectedMarketClock(
            is_open=bool(raw.is_open),
            timestamp=_aware(raw.timestamp),
            next_open=_aware(raw.next_open),
            next_close=_aware(raw.next_close),
        )


class SessionCalendarUnavailable(RuntimeError):
    """The authoritative broker session calendar could not be read."""


class SessionDateUnavailable(ValueError):
    """The exchange date is not a session in the authoritative calendar."""


class ExitOutsideSession(ValueError):
    """The proposed exit instant is outside the exchange session window."""


class AlpacaSessionCalendarAdapter:
    """Read-only Alpaca calendar boundary for validating a concrete exit instant."""

    _eastern = ZoneInfo("America/New_York")

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        environment: TradingEnvironment = TradingEnvironment.PAPER,
        client: Any | None = None,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("Alpaca credentials are required for the session calendar")
        self._client = client or TradingClient(
            api_key, secret_key, paper=environment is TradingEnvironment.PAPER
        )

    async def get_session(self, session_date: date) -> Any:
        try:
            rows = await asyncio.to_thread(
                self._client.get_calendar,
                GetCalendarRequest(start=session_date, end=session_date),
            )
        except Exception as error:
            raise SessionCalendarUnavailable("Alpaca session calendar read failed") from error
        for row in rows:
            if row.date == session_date:
                return row
        raise SessionDateUnavailable(
            f"No Alpaca exchange session exists for {session_date.isoformat()}"
        )

    async def validate_exit_in_session(self, proposed: datetime) -> date:
        if proposed.tzinfo is None or proposed.utcoffset() is None:
            raise ValueError("exit deadline must be timezone-aware")
        exchange_instant = proposed.astimezone(self._eastern)
        session_date = exchange_instant.date()
        session = await self.get_session(session_date)
        session_open = _calendar_aware(session.open)
        session_close = _calendar_aware(session.close)
        if not session_open <= proposed <= session_close:
            raise ExitOutsideSession(
                "Exit deadline must be within the Alpaca session open/close window"
            )
        return session_date


def _calendar_aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=AlpacaSessionCalendarAdapter._eastern)
            if value.tzinfo is None
            else value
        )
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=AlpacaSessionCalendarAdapter._eastern)
        if parsed.tzinfo is None
        else parsed
    )
