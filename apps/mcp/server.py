from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from fastmcp import FastMCP
except ImportError:  # keep imports/test discovery usable until the optional MCP extra is installed

    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self) -> Any:
            return lambda function: function

        def run(self) -> None:
            raise RuntimeError("Install fastmcp to run the MCP server")


mcp = FastMCP("AlphaDesk read-only assessment")


class MCPAssessmentError(RuntimeError):
    """Safe, normalized failures from the assessment API boundary."""


async def call_assessment_api(strategy: dict[str, Any]) -> dict[str, Any]:
    try:
        base_url = os.environ["ALPHADESK_API_URL"].rstrip("/")
        api_key = os.environ["ALPHADESK_API_KEY"]
    except KeyError as error:
        raise MCPAssessmentError("Assessment API configuration is missing") from error
    request = Request(
        f"{base_url}/api/v1/desk/strategy-assessments",
        data=json.dumps(strategy).encode(),
        headers={"Content-Type": "application/json", "X-AlphaDesk-API-Key": api_key},
        method="POST",
    )

    def send() -> dict[str, Any]:
        try:
            with urlopen(request, timeout=20) as response:
                status: int = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    if 400 <= status < 500:
                        message = "Assessment API rejected the request"
                    elif status >= 500:
                        message = "Assessment API is unavailable"
                    else:
                        message = "Assessment API returned an error"
                    raise MCPAssessmentError(message)
                document: object = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if 400 <= error.code < 500:
                message = "Assessment API rejected the request"
            elif error.code >= 500:
                message = "Assessment API is unavailable"
            else:
                message = "Assessment API returned an error"
            raise MCPAssessmentError(message) from error
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MCPAssessmentError("Assessment API returned an invalid response") from error

        if not isinstance(document, dict):
            raise MCPAssessmentError("Assessment API returned an invalid response")
        return cast(dict[str, Any], document)

    return await asyncio.to_thread(send)


@mcp.tool()
async def assess_options_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    """Assess an options strategy. This never creates approvals or orders."""
    return await call_assessment_api(strategy)


async def call_trade_assessment_api(trade: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlsplit

    import httpx

    from packages.connected.trade_assessment import TradeRequest, validate_pass_receipt

    proposal = TradeRequest.model_validate(trade)
    base = os.environ.get("ALPHADESK_API_URL", "").rstrip("/")
    key = os.environ.get("ALPHADESK_API_KEY", "")
    url = urlsplit(base)
    if (
        not key
        or url.username
        or url.password
        or not url.hostname
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise MCPAssessmentError("Assessment API configuration is missing or invalid")
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(
                f"{base}/api/v2/option-trade-assessments",
                headers={"X-AlphaDesk-API-Key": key},
                json=proposal.model_dump(mode="json"),
            )
            response.raise_for_status()
            if len(response.content) > 262144:
                raise MCPAssessmentError("Assessment API returned an invalid response")
            result = response.json()
        if (
            not isinstance(result, dict)
            or result.get("decision") not in {"PASS", "FAIL", "UNAVAILABLE"}
            or result.get("execution_allowed") is not False
        ):
            raise ValueError("invalid assessment")
        if result["decision"] == "PASS":
            if not result.get("assessment_id") or not validate_pass_receipt(result, proposal):
                raise ValueError("invalid PASS")
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise MCPAssessmentError(
            "Standalone assessment unavailable or invalid; do not execute this proposal"
        ) from exc


@mcp.tool()
async def assess_options_trade(
    legs: list[dict[str, Any]], quantity: int, limit_price: str, client_reference: str | None = None
) -> dict[str, Any]:
    """Assess your opening proposal without a scanner/watchlist. PASS is not broker authority;
    FAIL/UNAVAILABLE: inspect checks/remediation, skip this proposal and continue other work.
    Supported: long call/put or same-expiry 1:1 directional debit vertical. No orders or approvals.
    """
    return await call_trade_assessment_api(
        {
            "legs": legs,
            "quantity": quantity,
            "limit_price": limit_price,
            "client_reference": client_reference,
        }
    )


if __name__ == "__main__":
    mcp.run()
