from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel

from packages.ai.provider import AnthropicProvider, OpenRouterProvider, StructuredOutputError
from packages.ai.watchlist import WatchlistResearchReport


class Probe(BaseModel):
    status: str


class StrictProbe(BaseModel):
    status: Literal["ok"]


class FakeMessages:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response: object) -> None:
        self.messages = FakeMessages(response)


class RetryMessages:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.responses[len(self.calls) - 1]


class RetryClient:
    def __init__(self, responses: list[object]) -> None:
        self.messages = RetryMessages(responses)


class FakeCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.responses[len(self.calls) - 1]


class FakeOpenRouterClient:
    def __init__(self, responses: list[object]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


@pytest.mark.asyncio
async def test_openrouter_provider_retries_length_truncation() -> None:
    client = FakeOpenRouterClient(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"status":'), finish_reason="length"
                    )
                ],
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"status":"ok"}'))],
                finish_reason="stop",
            ),
        ]
    )
    provider = OpenRouterProvider("test-key", model="router-test", client=client)

    result = await provider.generate(
        agent_name="watchlist_research",
        instructions="Return status ok.",
        input_payload="{}",
        response_model=Probe,
    )

    assert result == Probe(status="ok")
    assert len(client.chat.completions.calls) == 2
    assert "\nReturn only" in client.chat.completions.calls[1]["messages"][0]["content"]
    assert (
        client.chat.completions.calls[1]["max_tokens"]
        > client.chat.completions.calls[0]["max_tokens"]
    )


@pytest.mark.asyncio
async def test_anthropic_provider_parses_schema_constrained_tool_output() -> None:
    client = FakeClient(
        SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"status": "ok"})])
    )
    provider = AnthropicProvider("test-key", model="claude-test", client=client)

    result = await provider.generate(
        agent_name="AlphaDeskCapabilityProbe",
        instructions="Return status ok.",
        input_payload='{"source":"test"}',
        response_model=Probe,
    )

    assert result == Probe(status="ok")
    request = client.messages.calls[0]
    assert request["model"] == "claude-test"
    assert request["tool_choice"] == {
        "type": "tool",
        "name": "AlphaDeskCapabilityProbe",
    }
    assert request["tools"][0]["input_schema"] == Probe.model_json_schema()


@pytest.mark.asyncio
async def test_anthropic_provider_rejects_missing_tool_output() -> None:
    client = FakeClient(SimpleNamespace(content=[]))
    provider = AnthropicProvider("test-key", model="claude-test", client=client)

    with pytest.raises(ValueError, match="no structured output"):
        await provider.generate(
            agent_name="probe",
            instructions="Return status ok.",
            input_payload="{}",
            response_model=Probe,
        )


@pytest.mark.asyncio
async def test_anthropic_provider_reports_safe_schema_diagnostics() -> None:
    client = FakeClient(
        SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", input={"status": "invalid"})],
        )
    )
    provider = AnthropicProvider("test-key", model="claude-test", client=client)

    with pytest.raises(StructuredOutputError) as error_info:
        await provider.generate(
            agent_name="probe",
            instructions="Return status ok.",
            input_payload="{}",
            response_model=StrictProbe,
        )

    assert error_info.value.diagnostics == [{"loc": "status", "type": "literal_error"}]
    assert error_info.value.stop_reason == "tool_use"


async def test_anthropic_provider_retries_truncated_tool_output_without_tool_block() -> None:
    client = RetryClient(
        [
            SimpleNamespace(stop_reason="max_tokens", content=[]),
            SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(type="tool_use", input={"status": "ok"})],
            ),
        ]
    )
    provider = AnthropicProvider("test-key", model="claude-test", client=client)

    result = await provider.generate(
        agent_name="watchlist_research",
        instructions="Return status ok.",
        input_payload="{}",
        response_model=Probe,
    )

    assert result == Probe(status="ok")
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_anthropic_provider_retries_truncated_tool_output_concisely() -> None:
    client = RetryClient(
        [
            SimpleNamespace(
                stop_reason="max_tokens",
                content=[SimpleNamespace(type="tool_use", input={"status": "invalid"})],
            ),
            SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(type="tool_use", input={"status": "ok"})],
            ),
        ]
    )
    provider = AnthropicProvider("test-key", model="claude-test", client=client)

    result = await provider.generate(
        agent_name="watchlist_research",
        instructions="Return status ok.",
        input_payload="{}",
        response_model=StrictProbe,
    )

    assert result == StrictProbe(status="ok")
    assert len(client.messages.calls) == 2
    assert client.messages.calls[1]["max_tokens"] > client.messages.calls[0]["max_tokens"]
    assert "Be concise" in str(client.messages.calls[1]["system"])


@pytest.mark.asyncio
async def test_anthropic_provider_retries_invalid_watchlist_payload_with_required_fields() -> None:
    client = RetryClient(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        input={"summary": "missing recommendations and as_of"},
                    )
                ],
            ),
            SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        input={
                            "summary": "Keep AAPL under review.",
                            "limitations": ["Point-in-time scan."],
                            "recommendations": [
                                {
                                    "symbol": "AAPL",
                                    "action": "WATCH",
                                    "rank": 1,
                                    "rationale": "The evidence is mixed.",
                                    "option_assessment": "INSUFFICIENT_DATA",
                                    "option_reason": "The scan is incomplete.",
                                    "risks": ["Evidence may become stale."],
                                    "confidence": 0.5,
                                    "citations": [
                                        {"source_id": "scan-AAPL", "claim": "Scan evidence."}
                                    ],
                                }
                            ],
                        },
                    )
                ],
            ),
        ]
    )
    provider = AnthropicProvider("test-key", model="claude-test", client=client)

    result = await provider.generate(
        agent_name="watchlist_research",
        instructions="Return the watchlist report.",
        input_payload="{}",
        response_model=WatchlistResearchReport,
    )

    assert result.recommendations[0].symbol == "AAPL"
    assert len(client.messages.calls) == 2
    retry_system = str(client.messages.calls[1]["system"])
    assert "every required field" in retry_system
    assert "at least one recommendation" in retry_system
    assert "omit as_of" in retry_system
