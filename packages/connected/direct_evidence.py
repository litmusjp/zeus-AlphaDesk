"""Read-only on-demand evidence. No scans, opportunities, LLMs or account equity."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from packages.domain.options import Greeks, OptionContract, OptionQuote
from packages.domain.workflow import CatalystFeatures


class EvidenceUnavailable(Exception):
    def __init__(self, code: str = "market_data_unavailable"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TradeEvidence:
    contracts: tuple[OptionContract, ...]
    features: CatalystFeatures
    features_at: datetime
    spot: Decimal
    greeks_at: datetime | None = None
    greeks_source: str | None = None
    market_data_feed: str = "indicative"
    market_data_quality: str = "testing_only"


def timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise EvidenceUnavailable("invalid_source_timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceUnavailable("invalid_source_timestamp")
    return parsed


def number(value: Any) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise EvidenceUnavailable("invalid_market_evidence")
    return parsed


def features_from_observations(
    stocks: dict,
    bars: list[dict],
    news: list[dict],
    underlying: str,
    liquidity: Decimal,
    *,
    calendar: list[dict] | None = None,
    now: datetime | None = None,
) -> tuple[CatalystFeatures, Decimal, datetime]:
    """Existing catalyst feature transformations, but with real source timestamps and liquidity."""
    now = now or datetime.now(UTC)
    market_zone = ZoneInfo("America/New_York")
    session = now.astimezone(market_zone).date()
    try:
        sessions = sorted(datetime.fromisoformat(row["date"]).date() for row in (calendar or []))
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceUnavailable("invalid_market_calendar_evidence") from exc
    prior_sessions = [day for day in sessions if day < session]
    if session not in sessions or not prior_sessions:
        raise EvidenceUnavailable("market_calendar_evidence_missing")
    previous_session = max(prior_sessions)

    def session_date(value):
        return timestamp(value).astimezone(market_zone).date()

    for symbol in {underlying, "SPY", "QQQ"}:
        snap = stocks[symbol]
        trade_time = timestamp(snap["latestTrade"]["t"])
        daily_time = timestamp(snap["dailyBar"]["t"])
        previous_time = timestamp(snap["prevDailyBar"]["t"])
        if (
            trade_time > now
            or daily_time > now
            or previous_time > now
            or session_date(trade_time) != session
            or session_date(daily_time) != session
            or session_date(previous_time) != previous_session
        ):
            raise EvidenceUnavailable("stale_constituent_evidence")
    bar_dates = [session_date(bar["t"]) for bar in bars]
    expected_dates = [day for day in prior_sessions if day >= session - timedelta(days=35)][-24:]
    if len(expected_dates) != 24:
        raise EvidenceUnavailable("historical_market_calendar_incomplete")
    historical_dates = sorted(day for day in bar_dates if day < session)
    if (
        len(bar_dates) != len(bars)
        or len(set(bar_dates)) != len(bar_dates)
        or historical_dates != expected_dates
        or any(day >= session for day in bar_dates)
    ):
        raise EvidenceUnavailable("stale_historical_evidence")
    for article in news:
        published = timestamp(article.get("created_at"))
        if not now - timedelta(hours=36) <= published <= now or underlying not in article.get(
            "symbols", []
        ):
            raise EvidenceUnavailable("invalid_news_evidence")
    stock = stocks[underlying]
    price = number(stock["latestTrade"]["p"])
    previous = number(stock["prevDailyBar"]["c"])
    opened = number(stock["dailyBar"]["o"])
    volume = number(stock["dailyBar"]["v"])
    # Current intraday volume must be compared with prior completed daily bars only.
    volumes = [number(b["v"]) for b in bars]
    if not volumes or min(price, previous, opened, volume, *volumes) <= 0:
        raise EvidenceUnavailable("historical_market_evidence_missing")
    average = sum(volumes) / len(volumes)
    if average <= 0:
        raise EvidenceUnavailable("historical_market_evidence_missing")
    positive = {
        "beats",
        "beat",
        "raises",
        "raised",
        "approval",
        "approved",
        "record",
        "growth",
        "wins",
    }
    negative = {
        "misses",
        "miss",
        "cuts",
        "cut",
        "lawsuit",
        "probe",
        "downgrade",
        "recall",
        "warning",
    }
    catalyst = (
        positive | negative | {"earnings", "guidance", "merger", "acquisition", "contract", "fda"}
    )
    words = " ".join(str(n["headline"]).lower() for n in news).split()
    positives = sum(word.strip(".,:;!?()") in positive for word in words)
    negatives = sum(word.strip(".,:;!?()") in negative for word in words)
    total = positives + negatives
    sentiment = Decimal(0) if total == 0 else Decimal(positives - negatives) / total
    catalyst_hits = sum(word.strip(".,:;!?()") in catalyst for word in words)
    confidence = min(
        Decimal(".15")
        + Decimal(len(news)) * Decimal(".06")
        + Decimal(catalyst_hits) * Decimal(".08"),
        Decimal(1),
    )

    def clamp(value):
        return min(max(value, Decimal(-1)), Decimal(1))

    def confirmation(symbol):
        snapshot = stocks[symbol]
        prior = number(snapshot["dailyBar"]["o"])
        latest = number(snapshot["latestTrade"]["p"])
        if min(prior, latest) <= 0:
            raise EvidenceUnavailable("benchmark_evidence_missing")
        return clamp((latest / prior - 1) * 20)

    observed = min(timestamp(stocks[s]["latestTrade"]["t"]) for s in {underlying, "SPY", "QQQ"})
    return (
        CatalystFeatures(
            catalyst_confidence=confidence,
            sentiment=sentiment,
            relative_volume=volume / average,
            price_momentum=clamp((price / opened - 1) * 20),
            gap_percent=(opened / previous - 1) * 100,
            market_confirmation=confirmation("SPY"),
            sector_confirmation=confirmation("QQQ"),
            liquidity_score=liquidity,
        ),
        price,
        observed,
    )


class DirectEvidenceProvider:
    def __init__(self, key: str, secret: str, *, transport=None):
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._transport = transport
        self._inflight: dict[tuple[str, ...], asyncio.Task[TradeEvidence]] = {}
        self._cache: OrderedDict[tuple[str, ...], tuple[float, TradeEvidence]] = OrderedDict()

    async def get_evidence(self, trade) -> TradeEvidence:
        symbols = tuple(sorted(leg.symbol for leg in trade.legs))
        cached = self._cache.get(symbols)
        if cached and monotonic() - cached[0] <= 1:
            return cached[1]
        try:
            task = self._inflight.get(symbols)
            if task is None:
                task = asyncio.create_task(self._fetch(symbols))
                self._inflight[symbols] = task

                def completed(done):
                    if self._inflight.get(symbols) is done:
                        self._inflight.pop(symbols, None)
                    if not done.cancelled():
                        done.exception()  # Consume failures when every caller timed out.

                task.add_done_callback(completed)
            evidence = await asyncio.shield(task)
        except EvidenceUnavailable:
            raise
        except (KeyError, TypeError, ValueError, ArithmeticError, httpx.HTTPError) as exc:
            raise EvidenceUnavailable() from exc
        self._cache[symbols] = (monotonic(), evidence)
        self._cache.move_to_end(symbols)
        if len(self._cache) > 128:
            self._cache.popitem(last=False)
        return evidence

    async def _fetch(self, symbols: tuple[str, ...]) -> TradeEvidence:
        underlying = symbols[0][:-15]
        start = (datetime.now(UTC) - timedelta(days=35)).isoformat()
        end = datetime.now(UTC).isoformat()
        async with httpx.AsyncClient(
            headers=self._headers, timeout=2, follow_redirects=False, transport=self._transport
        ) as client:

            async def get(url, params=None):
                response = await client.get(url, params=params)
                if response.status_code in {401, 403}:
                    raise EvidenceUnavailable("market_data_permission_denied")
                response.raise_for_status()
                return response.json()

            stocks, history, headlines, snapshots, calendar, *references = await asyncio.gather(
                get(
                    "https://data.alpaca.markets/v2/stocks/snapshots",
                    {"symbols": ",".join(sorted({underlying, "SPY", "QQQ"})), "feed": "iex"},
                ),
                get(
                    "https://data.alpaca.markets/v2/stocks/bars",
                    {
                        "symbols": underlying,
                        "timeframe": "1Day",
                        "start": start,
                        "end": end,
                        "limit": 25,
                        "sort": "desc",
                        "feed": "iex",
                    },
                ),
                get(
                    "https://data.alpaca.markets/v1beta1/news",
                    {
                        "symbols": underlying,
                        "start": (datetime.now(UTC) - timedelta(hours=36)).isoformat(),
                        "end": end,
                        "limit": 20,
                        "include_content": "false",
                    },
                ),
                get(
                    "https://data.alpaca.markets/v1beta1/options/snapshots",
                    {"symbols": ",".join(symbols), "feed": "indicative"},
                ),
                get(
                    "https://paper-api.alpaca.markets/v2/calendar",
                    {
                        "start": (datetime.now(UTC) - timedelta(days=35)).date().isoformat(),
                        "end": datetime.now(UTC).date().isoformat(),
                    },
                ),
                *(
                    get(f"https://paper-api.alpaca.markets/v2/options/contracts/{symbol}")
                    for symbol in symbols
                ),
            )
        contracts = []
        for symbol, ref in zip(symbols, references, strict=True):
            if ref.get("symbol") != symbol:
                raise EvidenceUnavailable("contract_reference_identity_mismatch")
            snapshot = snapshots["snapshots"][symbol]
            q = snapshot["latestQuote"]
            g = snapshot.get("greeks")
            oi = ref.get("open_interest")
            contracts.append(
                OptionContract(
                    contract_id=ref["id"],
                    symbol=ref["symbol"],
                    underlying_symbol=ref["underlying_symbol"],
                    expiration=ref["expiration_date"],
                    strike=number(ref["strike_price"]),
                    option_type=ref["type"],
                    multiplier=int(ref["size"]),
                    tradable=ref["tradable"] and ref["status"] == "active",
                    quote=OptionQuote(
                        bid=number(q["bp"]),
                        ask=number(q["ap"]),
                        bid_size=q["bs"],
                        ask_size=q["as"],
                        quoted_at=timestamp(q["t"]),
                        open_interest=int(oi) if oi is not None else None,
                        greeks=Greeks(
                            **{n: number(g[n]) for n in ("delta", "gamma", "theta", "vega")}
                        )
                        if g
                        and all(g.get(n) is not None for n in ("delta", "gamma", "theta", "vega"))
                        else None,
                    ),
                )
            )
        # Contract liquidity comes from the proposed contracts, never a placeholder score.
        spreads = [
            max(
                Decimal(0),
                min(
                    Decimal(1),
                    c.quote.spread_ratio if c.quote.spread_ratio is not None else Decimal(1),
                ),
            )
            for c in contracts
        ]
        features, spot, observed = features_from_observations(
            stocks,
            history["bars"][underlying],
            headlines["news"],
            underlying,
            Decimal(1) - max(spreads),
            calendar=calendar,
        )
        return TradeEvidence(tuple(contracts), features, observed, spot)
