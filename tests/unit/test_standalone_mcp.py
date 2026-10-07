import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import apps.mcp.server as server
from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.trade_assessment import (
    TradeRequest,
    policy_metadata,
    proposal_fingerprint,
    validate_pass_receipt,
)
from packages.domain.workflow import CatalystFeatures
from packages.strategy.catalyst import score_components, score_signal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "failed_check",
        "blocker",
        "missing_metadata",
        "bad_time",
        "bad_fingerprint",
        "retryable",
        "unknown_trade_field",
        "string_score",
        "bool_component",
        "contradictory_pass",
    ],
)
async def test_new_tool_calls_the_same_api_through_real_mcp_protocol(monkeypatch, malformed):
    now = datetime.now(UTC)
    body_seen = []
    original = httpx.AsyncClient

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v2/option-trade-assessments"
        assert request.headers["X-AlphaDesk-API-Key"] == "test-fixture-only"
        body = json.loads(request.content)
        body_seen.append(body)
        proposal = TradeRequest.model_validate(body)
        features = CatalystFeatures(
            catalyst_confidence="1", sentiment="1", relative_volume="3",
            price_momentum="1", gap_percent="0", market_confirmation="1",
            sector_confirmation="1", liquidity_score="1",
        )
        policy = AssessmentPolicy()
        doc = {
            **policy_metadata(policy),
            "decision": "PASS",
            "signal_score": float(score_signal(features)),
            "execution_allowed": False,
            "trade": proposal.model_dump(mode="json"),
            "trade_fingerprint": proposal_fingerprint(proposal),
            "expires_at": (now + timedelta(seconds=30)).isoformat(),
            "checks": [],
            "blocking_reasons": [],
            "remediation": [],
            "score_source": "alphadesk_direct_market_data",
            "market_data": {
                "source": "alpaca",
                "feed": "indicative",
                "quality": "testing_only",
                "greeks_calculation_provenance": None,
                "greeks_calculated_at": None,
                "greeks_calculation_freshness": "unknown",
                "warnings": ["greek_calculation_time_unknown"],
                "source_quote_times": {"AAPL261106C00200000": now.isoformat()},
            },
            "score_profile": "catalyst_momentum_v1",
            "score_components": {
                name: float(value) for name, value in score_components(features).items()
            },
            "retryable": False,
            "assessment_id": "synthetic-only",
            "observed_at": now.isoformat(),
            "evidence_observed_at": now.isoformat(),
            "evidence_as_of": now.isoformat(),
        }
        doc["checks"] = [{"code": "quality", "passed": True}]
        assert validate_pass_receipt(doc, proposal, now=now), doc
        if malformed == "failed_check":
            doc["checks"][0]["passed"] = False
        if malformed == "blocker":
            doc["blocking_reasons"] = ["criterion failed"]
        if malformed == "missing_metadata":
            doc.pop("observed_at")
        if malformed == "bad_time":
            doc["evidence_observed_at"] = (now + timedelta(hours=1)).isoformat()
        if malformed == "bad_fingerprint":
            doc["trade_fingerprint"] = "forged"
        if malformed == "retryable":
            doc["retryable"] = True
        if malformed == "unknown_trade_field":
            doc["trade"]["authority"] = True
        if malformed == "string_score":
            doc["signal_score"] = "80"
        if malformed == "bool_component":
            doc["score_components"]["sentiment"] = True
        if malformed == "contradictory_pass":
            doc["pass"] = False
        return httpx.Response(200, json=doc)

    def configured(*args, **kwargs):
        return original(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", configured)
    monkeypatch.setenv("ALPHADESK_API_URL", "https://mcp-fixture.invalid")
    monkeypatch.setenv("ALPHADESK_API_KEY", "test-fixture-only")
    async with Client(server.mcp) as client:
        names = [tool.name for tool in await client.list_tools()]
        assert "assess_options_trade" in names
        if malformed:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "assess_options_trade",
                    {
                        "legs": [{"symbol": "AAPL261106C00200000", "side": "buy"}],
                        "quantity": 1,
                        "limit_price": "2",
                    },
                )
            return
        result = await client.call_tool(
            "assess_options_trade",
            {
                "legs": [{"symbol": "AAPL261106C00200000", "side": "buy"}],
                "quantity": 1,
                "limit_price": "2",
            },
        )
    assert not result.is_error
    forwarded = json.loads(result.content[0].text)
    assert forwarded["decision"] == "PASS"
    assert forwarded["market_data"]["feed"] == "indicative"
    assert forwarded["market_data"]["source_quote_times"] == {
        "AAPL261106C00200000": now.isoformat()
    }
    assert len(body_seen) == 1
    assert set(body_seen[0]) == {"legs", "quantity", "limit_price", "client_reference"}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["FAIL", "UNAVAILABLE"])
async def test_mcp_preserves_nonpassing_feedback_without_promoting_it(monkeypatch, decision):
    original = httpx.AsyncClient

    def handler(request):
        return httpx.Response(
            200,
            json={
                "decision": decision,
                "execution_allowed": False,
                "signal_score": None if decision == "UNAVAILABLE" else 40,
                "blocking_reasons": ["fixture_reason"],
                "remediation": ["specific fixture feedback"],
                "market_data": {
                    "source": "alpaca",
                    "feed": "indicative",
                    "quality": "testing_only",
                    "greeks_calculation_provenance": None,
                    "source_quote_times": None,
                },
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(*args, **kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("ALPHADESK_API_URL", "https://mcp-fixture.invalid")
    monkeypatch.setenv("ALPHADESK_API_KEY", "test-fixture-only")
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "assess_options_trade",
            {
                "legs": [{"symbol": "AAPL261106C00200000", "side": "buy"}],
                "quantity": 1,
                "limit_price": "2",
            },
        )
    payload = json.loads(result.content[0].text)
    assert payload["decision"] == decision
    assert payload["blocking_reasons"] == ["fixture_reason"]
    assert payload["remediation"] == ["specific fixture feedback"]
