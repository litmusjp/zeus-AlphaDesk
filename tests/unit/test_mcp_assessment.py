import inspect
import json
from urllib.error import HTTPError

import pytest

import apps.mcp.server as server
from apps.mcp.server import MCPAssessmentError, assess_options_strategy, call_assessment_api


def test_mcp_contract_is_read_only_and_uses_environment_configuration() -> None:
    assert "ALPHADESK_API_URL" in inspect.getsource(call_assessment_api)
    assert "ALPHADESK_API_KEY" in inspect.getsource(call_assessment_api)
    assert (
        getattr(assess_options_strategy, "name", "assess_options_strategy")
        == "assess_options_strategy"
    )


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


@pytest.mark.asyncio
async def test_mcp_rejects_non_json_without_exposing_response_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPHADESK_API_URL", "https://api.example.test")
    monkeypatch.setenv("ALPHADESK_API_KEY", "secret-value")
    monkeypatch.setattr(server, "urlopen", lambda _request, timeout: _Response(b"not-json"))

    with pytest.raises(MCPAssessmentError, match="invalid response") as raised:
        await call_assessment_api({"strategy_type": "vertical"})

    assert "secret-value" not in str(raised.value)
    assert "not-json" not in str(raised.value)


@pytest.mark.asyncio
async def test_mcp_normalizes_http_errors_without_exposing_headers_or_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPHADESK_API_URL", "https://api.example.test")
    monkeypatch.setenv("ALPHADESK_API_KEY", "secret-value")
    error = HTTPError(
        "https://api.example.test/api/v1/desk/strategy-assessments",
        401,
        "secret-body",
        {"X-Leaky": "secret-header"},
        None,
    )
    monkeypatch.setattr(server, "urlopen", lambda _request, timeout: (_ for _ in ()).throw(error))

    with pytest.raises(MCPAssessmentError, match="rejected the request") as raised:
        await call_assessment_api({})

    assert "secret-value" not in str(raised.value)
    assert "secret-body" not in str(raised.value)
    assert "secret-header" not in str(raised.value)


@pytest.mark.asyncio
async def test_mcp_accepts_only_json_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHADESK_API_URL", "https://api.example.test")
    monkeypatch.setenv("ALPHADESK_API_KEY", "secret-value")
    monkeypatch.setattr(
        server,
        "urlopen",
        lambda _request, timeout: _Response(json.dumps(["unexpected-list"]).encode()),
    )

    with pytest.raises(MCPAssessmentError, match="invalid response"):
        await call_assessment_api({})


@pytest.mark.asyncio
async def test_mcp_normalizes_non_2xx_response_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHADESK_API_URL", "https://api.example.test")
    monkeypatch.setenv("ALPHADESK_API_KEY", "secret-value")
    monkeypatch.setattr(
        server, "urlopen", lambda _request, timeout: _Response(b"secret-body", status=503)
    )

    with pytest.raises(MCPAssessmentError, match="unavailable") as raised:
        await call_assessment_api({})

    assert "secret-body" not in str(raised.value)
