from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from packages.connected.direct_evidence import EvidenceUnavailable, features_from_observations

NOW = datetime(2026, 10, 6, 15, tzinfo=UTC)


def evidence():
    stocks = {
        s: {
            "latestTrade": {"p": 205, "t": NOW.isoformat()},
            "dailyBar": {"o": 200, "v": 3000, "t": "2026-10-06T04:00:00Z"},
            "prevDailyBar": {"c": 200, "t": "2026-10-05T04:00:00Z"},
        }
        for s in ("AAPL", "SPY", "QQQ")
    }
    bars = [{"v": 1000, "t": "2026-10-05T04:00:00Z"}]
    news = [
        {
            "headline": "earnings beat record growth",
            "created_at": NOW.isoformat(),
            "symbols": ["AAPL"],
        }
    ]
    return stocks, bars, news


@pytest.mark.parametrize(
    "bad",
    [
        "daily",
        "benchmark",
        "previous",
        "history",
        "news_old",
        "news_future",
        "unrelated_news",
        "missing_news_time",
    ],
)
def test_fresh_trades_cannot_launder_invalid_constituents(bad):
    stocks, bars, news = deepcopy(evidence())
    if bad == "daily":
        stocks["AAPL"]["dailyBar"]["t"] = (NOW - timedelta(days=7)).isoformat()
    elif bad == "benchmark":
        stocks["SPY"]["dailyBar"]["t"] = (NOW - timedelta(days=7)).isoformat()
    elif bad == "previous":
        stocks["AAPL"]["prevDailyBar"]["t"] = NOW.isoformat()
    elif bad == "history":
        bars[0]["t"] = (NOW - timedelta(days=7)).isoformat()
    elif bad == "news_old":
        news[0]["created_at"] = "2020-01-01T00:00:00Z"
    elif bad == "news_future":
        news[0]["created_at"] = (NOW + timedelta(hours=1)).isoformat()
    elif bad == "unrelated_news":
        news[0]["symbols"] = ["UNRELATED"]
    else:
        news[0].pop("created_at")
    with pytest.raises(EvidenceUnavailable):
        features_from_observations(
            stocks,
            bars,
            news,
            "AAPL",
            Decimal(".95"),
            calendar=[{"date": "2026-10-05"}, {"date": "2026-10-06"}],
            now=NOW,
        )
