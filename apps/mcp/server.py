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


if __name__ == "__main__":
    mcp.run()
