from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.ai.provider import FixtureAIProvider
from packages.ai.watchlist import (
    DISCOVERY_UNIVERSE,
    WatchlistResearchReport,
    _compact_evidence,
    run_watchlist_research,
)


def test_discovery_universe_includes_existing_symbols_and_additional_candidates() -> None:
    assert {"AAPL", "AMZN", "GOOG", "META", "MSFT", "NVDA", "QQQ", "SPY", "TSLA"}.issubset(
        DISCOVERY_UNIVERSE
    )
    assert len(DISCOVERY_UNIVERSE) >= 20
    assert "PLTR" in DISCOVERY_UNIVERSE
    assert (
        WatchlistResearchReport.model_json_schema()["properties"]["recommendations"]["maxItems"]
        == 10
    )


def test_watchlist_research_compacts_option_evidence_without_losing_contract_identity() -> None:
    compacted = _compact_evidence(
        (
            {
                "source_id": "scan-AAPL",
                "symbol": "AAPL",
                "status": "AVAILABLE",
                "candidate": {
                    "structure_id": "structure-1",
                    "structure": {
                        "structure_type": "bull_call_debit_spread",
                        "legs": [
                            {
                                "side": "long",
                                "ratio": 1,
                                "contract": {
                                    "contract_id": "contract-1",
                                    "symbol": "AAPL250117C00200000",
                                    "quote": {"bid": "1", "ask": "2"},
                                },
                            }
                        ],
                    },
                },
            },
        )
    )

    candidate = compacted[0]["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["structure"]["legs"][0]["contract"]["contract_id"] == "contract-1"
    assert "quote" not in candidate["structure"]["legs"][0]["contract"]


@pytest.mark.asyncio
async def test_watchlist_research_returns_schema_valid_ranked_advice() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "Favor diversified leaders while keeping concentration risk visible.",
                "limitations": ["The evidence is a point-in-time scan."],
                "recommendations": [
                    {
                        "symbol": "AAPL",
                        "action": "KEEP",
                        "rank": 1,
                        "rationale": "Positive momentum with liquid market data.",
                        "option_assessment": "REVIEW_ONLY",
                        "option_reason": (
                            "The scan did not establish a current executable contract."
                        ),
                        "risks": ["Large-cap technology concentration."],
                        "confidence": "0.78",
                        "citations": [
                            {"source_id": "scan-AAPL", "claim": "AAPL had a positive scan signal."}
                        ],
                    }
                ],
                "as_of": datetime.now(UTC).isoformat(),
            }
        }
    )

    result = await run_watchlist_research(
        provider,
        symbols=("AAPL", "MSFT"),
        evidence=(
            {
                "source_id": "scan-AAPL",
                "symbol": "AAPL",
                "status": "AVAILABLE",
                "observed_at": datetime(2026, 9, 17, 12, 0, tzinfo=UTC).isoformat(),
                "signal": {"score": 78},
            },
            {
                "source_id": "scan-MSFT",
                "symbol": "MSFT",
                "status": "AVAILABLE",
                "observed_at": datetime(2026, 9, 17, 12, 0, tzinfo=UTC).isoformat(),
                "signal": {"score": 61},
            },
        ),
    )

    assert result.recommendations[0].symbol == "AAPL"
    assert result.recommendations[0].action == "KEEP"
    assert result.recommendations[0].citations[0].source_id == "scan-AAPL"
    assert result.as_of.tzinfo is not None


@pytest.mark.asyncio
async def test_watchlist_research_accepts_provider_report_without_as_of() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "Keep the symbol under review.",
                "limitations": ["The evidence is a point-in-time scan."],
                "recommendations": [
                    {
                        "symbol": "AAPL",
                        "action": "WATCH",
                        "rank": 1,
                        "rationale": "The available scan evidence is mixed.",
                        "option_assessment": "INSUFFICIENT_DATA",
                        "option_reason": "The scan did not establish option suitability.",
                        "risks": ["Evidence may become stale."],
                        "confidence": 0.5,
                        "citations": [{"source_id": "scan-AAPL", "claim": "Scan evidence."}],
                    }
                ],
            }
        }
    )

    result = await run_watchlist_research(
        provider,
        symbols=("AAPL",),
        evidence=({"source_id": "scan-AAPL", "symbol": "AAPL", "status": "AVAILABLE"},),
    )

    assert result.as_of is None


@pytest.mark.asyncio
async def test_watchlist_research_rejects_empty_recommendations() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "No recommendations.",
                "limitations": ["The evidence is incomplete."],
                "recommendations": [],
            }
        }
    )

    with pytest.raises(ValidationError):
        await run_watchlist_research(
            provider,
            symbols=("AAPL",),
            evidence=({"source_id": "scan-AAPL", "symbol": "AAPL", "status": "AVAILABLE"},),
        )


@pytest.mark.asyncio
async def test_watchlist_research_rejects_unknown_citations() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "Summary",
                "limitations": ["Limited evidence."],
                "recommendations": [
                    {
                        "symbol": "AAPL",
                        "action": "KEEP",
                        "rank": 1,
                        "rationale": "Reason",
                        "option_assessment": "INSUFFICIENT_DATA",
                        "option_reason": "Evidence is incomplete.",
                        "risks": ["Risk"],
                        "confidence": "0.5",
                        "citations": [{"source_id": "not-provided", "claim": "Unsupported"}],
                    }
                ],
                "as_of": datetime.now(UTC).isoformat(),
            }
        }
    )

    with pytest.raises(ValueError, match="unknown sources"):
        await run_watchlist_research(
            provider,
            symbols=("AAPL",),
            evidence=({"source_id": "scan-AAPL", "symbol": "AAPL", "status": "AVAILABLE"},),
        )


@pytest.mark.asyncio
async def test_watchlist_research_rejects_cross_symbol_citations() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "Summary",
                "limitations": ["Limited evidence."],
                "recommendations": [
                    {
                        "symbol": "AAPL",
                        "action": "WATCH",
                        "rank": 1,
                        "rationale": "Reason",
                        "option_assessment": "REVIEW_ONLY",
                        "option_reason": "The evidence is mixed.",
                        "risks": ["Risk"],
                        "confidence": "0.5",
                        "citations": [{"source_id": "scan-MSFT", "claim": "Unsupported"}],
                    }
                ],
                "as_of": datetime.now(UTC).isoformat(),
            }
        }
    )

    with pytest.raises(ValueError, match="unrelated"):
        await run_watchlist_research(
            provider,
            symbols=("AAPL", "MSFT"),
            evidence=(
                {"source_id": "scan-AAPL", "symbol": "AAPL", "status": "AVAILABLE"},
                {"source_id": "scan-MSFT", "symbol": "MSFT", "status": "AVAILABLE"},
            ),
        )


@pytest.mark.asyncio
async def test_watchlist_research_rejects_unsupported_execution_eligibility() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "Summary",
                "limitations": ["Point-in-time evidence."],
                "recommendations": [
                    {
                        "symbol": "AAPL",
                        "action": "WATCH",
                        "rank": 1,
                        "rationale": "Reason",
                        "option_assessment": "EXECUTION_ELIGIBLE",
                        "option_reason": "The option is ready.",
                        "risks": ["Risk"],
                        "confidence": "0.5",
                        "citations": [{"source_id": "scan-AAPL", "claim": "AAPL scan."}],
                    }
                ],
                "as_of": datetime.now(UTC).isoformat(),
            }
        }
    )

    with pytest.raises(ValueError, match="overstated option eligibility"):
        await run_watchlist_research(
            provider,
            symbols=("AAPL",),
            evidence=(
                {
                    "source_id": "scan-AAPL",
                    "symbol": "AAPL",
                    "status": "AVAILABLE",
                    "option_diagnostics": {
                        "strict_eligible_contracts": 2,
                        "selected_structure_strictly_eligible": 0,
                    },
                    "risk_decision": {"decision": "APPROVE"},
                },
            ),
        )


@pytest.mark.asyncio
async def test_watchlist_research_allows_citations_for_unavailable_symbols() -> None:
    provider = FixtureAIProvider(
        {
            "watchlist_research": {
                "summary": "The symbol needs more data.",
                "limitations": ["The real-data scan was unavailable."],
                "recommendations": [
                    {
                        "symbol": "AAPL",
                        "action": "WATCH",
                        "rank": 1,
                        "rationale": "Keep under observation until data is available.",
                        "option_assessment": "INSUFFICIENT_DATA",
                        "option_reason": "No current deterministic scan evidence.",
                        "risks": ["Evidence unavailable."],
                        "confidence": "0.2",
                        "citations": [{"source_id": "scan-AAPL", "claim": "Scan unavailable."}],
                    }
                ],
                "as_of": datetime.now(UTC).isoformat(),
            }
        }
    )

    result = await run_watchlist_research(
        provider,
        symbols=("AAPL",),
        evidence=(
            {
                "source_id": "scan-AAPL",
                "symbol": "AAPL",
                "status": "UNAVAILABLE_FROM_DISCOVERY_SCAN",
            },
        ),
    )

    assert result.recommendations[0].option_assessment == "INSUFFICIENT_DATA"
