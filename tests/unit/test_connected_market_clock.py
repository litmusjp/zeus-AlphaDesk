from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from packages.connected.market_clock import AlpacaMarketClockAdapter, ConnectedMarketClock
from packages.execution.conditional_runner import (
    _opening_clock_allows_submission,
    _opening_market_clock_failure_reason,
)


@pytest.mark.asyncio
async def test_market_clock_is_normalized_without_alpaca_types() -> None:
    client = SimpleNamespace(
        get_clock=lambda: SimpleNamespace(
            is_open=True,
            timestamp=datetime(2026, 9, 3, 14, 0, tzinfo=UTC),
            next_open=datetime(2026, 9, 4, 13, 30, tzinfo=UTC),
            next_close=datetime(2026, 9, 3, 20, 0, tzinfo=UTC),
        )
    )
    adapter = AlpacaMarketClockAdapter("paper-key", "paper-secret", client=client)

    clock = await adapter.get_clock()

    assert clock.is_open
    assert clock.source == "ALPACA_REAL"
    assert clock.timezone == "America/New_York"
    assert clock.next_close == datetime(2026, 9, 3, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        (
            ConnectedMarketClock(
                is_open=True,
                timestamp=datetime(2026, 9, 21, 14, 0, tzinfo=UTC),
                next_open=datetime(2026, 9, 22, 13, 30, tzinfo=UTC),
                next_close=datetime(2026, 9, 21, 20, 0, tzinfo=UTC),
            ),
            True,
        ),
        (
            ConnectedMarketClock(
                is_open=False,
                timestamp=datetime(2026, 9, 21, 21, 0, tzinfo=UTC),
                next_open=datetime(2026, 9, 22, 13, 30, tzinfo=UTC),
                next_close=datetime(2026, 9, 21, 20, 0, tzinfo=UTC),
            ),
            False,
        ),
        (None, False),
    ],
)
def test_opening_requires_authoritative_broker_clock(
    clock: ConnectedMarketClock | None, expected: bool
) -> None:
    assert _opening_clock_allows_submission(clock) is expected


@pytest.mark.parametrize(
    ("clock", "reason"),
    [
        (None, "authoritative_market_clock_unavailable"),
        (
            ConnectedMarketClock(
                is_open=False,
                timestamp=datetime(2026, 9, 21, 21, 0, tzinfo=UTC),
                next_open=datetime(2026, 9, 22, 13, 30, tzinfo=UTC),
                next_close=datetime(2026, 9, 21, 20, 0, tzinfo=UTC),
            ),
            "market_session_closed",
        ),
    ],
)
def test_opening_clock_failure_is_retryable_before_authorization(
    clock: ConnectedMarketClock | None, reason: str
) -> None:
    assert _opening_market_clock_failure_reason(clock) == reason


def test_opening_clock_does_not_block_authorization_when_open() -> None:
    clock = ConnectedMarketClock(
        is_open=True,
        timestamp=datetime(2026, 9, 21, 14, 0, tzinfo=UTC),
        next_open=datetime(2026, 9, 22, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 9, 21, 20, 0, tzinfo=UTC),
    )

    assert _opening_market_clock_failure_reason(clock) is None
