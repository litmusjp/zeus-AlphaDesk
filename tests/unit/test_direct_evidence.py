import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

import packages.connected.direct_evidence as direct_evidence
from packages.connected.direct_evidence import (
    DirectEvidenceProvider,
    EvidenceUnavailable,
    features_from_observations,
)
from packages.connected.trade_assessment import TradeRequest


def test_historical_volume_requires_the_complete_requested_session_window():
    now = datetime(2026, 1, 12, 15, tzinfo=UTC)
    snapshot = {
        "latestTrade": {"p": 101, "t": now.isoformat()},
        "dailyBar": {"o": 100, "v": 500, "t": now.isoformat()},
        "prevDailyBar": {"c": 100, "t": datetime(2026, 1, 9, 15, tzinfo=UTC).isoformat()},
    }
    with pytest.raises(EvidenceUnavailable):
        features_from_observations(
            {symbol: snapshot for symbol in ("AAPL", "SPY", "QQQ")},
            [{"t": "2026-01-09T15:00:00+00:00", "v": 1000}],
            [],
            "AAPL",
            Decimal("1"),
            calendar=[{"date": day} for day in ("2026-01-09", "2026-01-12")],
            now=now,
        )


@pytest.mark.parametrize(
    "mutation", ["omitted", "duplicate", "current", "future", "invalid_time", "valid"]
)
def test_calendar_coverage_accepts_only_24_completed_provider_sessions(mutation):
    now = datetime(2026, 2, 3, 15, tzinfo=UTC)
    from datetime import date

    expected = []
    cursor = date(2026, 2, 2)
    while len(expected) < 24:
        if cursor.weekday() < 5 and cursor.isoformat() != "2026-01-19":
            expected.append(cursor)
        cursor -= timedelta(days=1)
    expected.reverse()
    bars = [
        {"v": 1000, "t": datetime(d.year, d.month, d.day, 15, tzinfo=UTC).isoformat()}
        for d in expected
    ]
    if mutation == "omitted":
        bars.pop(3)
    elif mutation == "duplicate":
        bars[4] = dict(bars[3])
    elif mutation == "future":
        bars.append({"v": 1000, "t": (now + timedelta(days=1)).isoformat()})
    elif mutation == "current":
        bars.append({"v": 1000, "t": now.isoformat()})
    elif mutation == "invalid_time":
        bars[0]["t"] = "not-a-timestamp"
    bars.reverse()
    prior = expected[-1]
    prev = datetime(prior.year, prior.month, prior.day, 15, tzinfo=UTC).isoformat()
    snapshot = {
        "latestTrade": {"p": 101, "t": now.isoformat()},
        "dailyBar": {"o": 100, "v": 500, "t": now.isoformat()},
        "prevDailyBar": {"c": 100, "t": prev},
    }
    kwargs = dict(
        stocks={symbol: snapshot for symbol in ("AAPL", "SPY", "QQQ")},
        bars=bars,
        news=[],
        underlying="AAPL",
        liquidity=Decimal("1"),
        calendar=[{"date": d.isoformat()} for d in expected] + [{"date": now.date().isoformat()}],
        now=now,
    )
    if mutation == "valid":
        features_from_observations(**kwargs)
    else:
        with pytest.raises(EvidenceUnavailable):
            features_from_observations(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "reference_symbol",
        "benchmark_zero",
        "benchmark_negative",
        "bad_bar_volume",
        "bad_intraday_volume",
        "zero_price",
    ],
)
@pytest.mark.parametrize(
    "now,holidays",
    [
        (datetime(2026, 8, 28, 16, tzinfo=UTC), set()),
        (datetime(2026, 1, 26, 16, tzinfo=UTC), {"2025-12-25", "2026-01-01", "2026-01-19"}),
    ],
)
async def test_parallel_get_only_market_evidence_preserves_times_and_has_bounded_cache(
    invalid, now, holidays, monkeypatch
):

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(direct_evidence, "datetime", FrozenDateTime)
    expiry = now + timedelta(days=30)
    symbol = f"AAPL{expiry.strftime('%y%m%d')}C00200000"
    seen = []

    async def handler(request):
        assert request.method == "GET"
        assert request.headers["APCA-API-KEY-ID"] == "fixture-only"
        seen.append(request.url.path)
        await asyncio.sleep(0.01)
        path = request.url.path
        from zoneinfo import ZoneInfo

        market_day = now.astimezone(ZoneInfo("America/New_York")).date()
        previous_session = market_day - timedelta(days=1)
        while previous_session.weekday() >= 5 or previous_session.isoformat() in holidays:
            previous_session -= timedelta(days=1)
        stock = {
            "latestTrade": {"p": 205, "t": now.isoformat()},
            "dailyBar": {"o": 200, "v": 3000, "t": now.isoformat()},
            "prevDailyBar": {
                "c": 200,
                "t": datetime.combine(
                    previous_session, datetime.min.time(), ZoneInfo("America/New_York")
                ).isoformat(),
            },
        }
        if invalid == "bad_intraday_volume":
            stock["dailyBar"]["v"] = -1
        if invalid == "zero_price":
            stock["latestTrade"]["p"] = 0
        if path.endswith("/stocks/snapshots"):
            result = {s: deepcopy(stock) for s in ["AAPL", "SPY", "QQQ"]}
            if invalid in {"benchmark_zero", "benchmark_negative"}:
                result["SPY"]["latestTrade"]["p"] = 0 if invalid == "benchmark_zero" else -1
        elif path.endswith("/stocks/bars"):
            assert request.url.params["symbols"] == "AAPL"
            assert request.url.params["timeframe"] == "1Day"
            assert request.url.params["limit"] == "24"
            assert request.url.params["sort"] == "desc"
            assert request.url.params["feed"] == "iex"
            from zoneinfo import ZoneInfo

            market_zone = ZoneInfo("America/New_York")
            day = now.astimezone(ZoneInfo("America/New_York")).date()
            assert (
                request.url.params["start"]
                == datetime.combine(
                    day - timedelta(days=60), datetime.min.time(), market_zone
                ).isoformat()
            )
            assert datetime.fromisoformat(request.url.params["end"]) < datetime.combine(
                day, datetime.min.time(), market_zone
            )
            prior = []
            cursor = day - timedelta(days=1)
            query_start = datetime.fromisoformat(request.url.params["start"]).date()
            while cursor >= query_start:
                if cursor.weekday() < 5 and cursor.isoformat() not in holidays:
                    prior.append(cursor)
                cursor -= timedelta(days=1)
            limit = int(request.url.params["limit"])
            result = {
                "bars": {
                    "AAPL": [
                        {
                            "v": 1000,
                            "t": datetime.combine(
                                d, datetime.min.time(), ZoneInfo("America/New_York")
                            ).isoformat(),
                        }
                        for d in prior[:limit]
                    ]
                }
            }
            if invalid == "bad_bar_volume":
                result["bars"]["AAPL"][0]["v"] = -1
        elif path.endswith("/news"):
            assert request.url.params["start"] == (now - timedelta(hours=36)).isoformat()
            assert request.url.params["end"] == now.isoformat()
            result = {
                "news": [
                    {
                        "headline": "earnings beat record growth raises approval",
                        "created_at": now.isoformat(),
                        "symbols": ["AAPL"],
                    }
                ]
                * 6
            }
        elif path.endswith("/calendar"):
            from zoneinfo import ZoneInfo

            day = now.astimezone(ZoneInfo("America/New_York")).date()
            dates = [day]
            cursor = day - timedelta(days=1)
            assert request.url.params["start"] == (day - timedelta(days=60)).isoformat()
            assert request.url.params["end"] == day.isoformat()
            while cursor >= day - timedelta(days=60):
                if cursor.weekday() < 5 and cursor.isoformat() not in holidays:
                    dates.append(cursor)
                cursor -= timedelta(days=1)
            result = [{"date": item.isoformat()} for item in reversed(dates)]
        elif path.endswith("/options/snapshots"):
            assert request.url.params["feed"] == "indicative"
            result = {
                "snapshots": {
                    symbol: {
                        "latestQuote": {
                            "bp": 1.95,
                            "ap": 2.05,
                            "bs": 10,
                            "as": 10,
                            "t": now.isoformat(),
                        },
                        "greeks": {"delta": 0.5, "gamma": 0.01, "theta": -0.01, "vega": 0.1},
                    }
                }
            }
        elif path.endswith("/contracts/" + symbol):
            result = {
                "id": "fixture-contract",
                "symbol": symbol,
                "underlying_symbol": "AAPL",
                "expiration_date": expiry.date().isoformat(),
                "strike_price": "200",
                "type": "call",
                "size": "100",
                "tradable": True,
                "status": "active",
                "open_interest": "1000",
            }
        else:
            raise AssertionError(f"Unexpected non-market endpoint {path}")
        if invalid == "reference_symbol" and path.endswith("/contracts/" + symbol):
            result["symbol"] = f"AAPL{expiry.strftime('%y%m%d')}C00210000"
        return httpx.Response(200, json=result)

    provider = DirectEvidenceProvider(
        "fixture-only", "fixture-not-a-secret", transport=httpx.MockTransport(handler)
    )
    trade = TradeRequest.model_validate(
        {"legs": [{"symbol": symbol, "side": "buy"}], "quantity": 1, "limit_price": "2"}
    )
    if invalid:
        with pytest.raises(EvidenceUnavailable):
            await provider.get_evidence(trade)
        return
    first, concurrent = await asyncio.gather(
        provider.get_evidence(trade), provider.get_evidence(trade)
    )
    assert concurrent is first
    assert first.features_at == now
    assert first.greeks_at is None and first.greeks_source is None
    assert first.contracts[0].quote.quoted_at == now
    assert first.market_data_feed == "indicative"
    assert first.market_data_quality == "testing_only"
    assert first.features.liquidity_score != 0.75
    assert len(seen) == 6
    assert await provider.get_evidence(trade) is first
    assert len(seen) == 6


@pytest.mark.asyncio
async def test_permission_denial_is_specific_sanitized_and_has_no_fallback():
    async def denied(request):
        return httpx.Response(403, json={"message": "fixture-not-a-secret"})

    provider = DirectEvidenceProvider(
        "fixture-only", "fixture-not-a-secret", transport=httpx.MockTransport(denied)
    )
    trade = TradeRequest.model_validate(
        {
            "legs": [{"symbol": "AAPL261106C00200000", "side": "buy"}],
            "quantity": 1,
            "limit_price": "2",
        }
    )
    with pytest.raises(EvidenceUnavailable) as error:
        await provider.get_evidence(trade)
    assert error.value.code == "market_data_permission_denied"
    assert "fixture-not-a-secret" not in str(error.value)
