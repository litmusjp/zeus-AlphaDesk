from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol, TypeVar, cast

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class StructuredOutputError(ValueError):
    """The provider returned content that was not a schema-valid JSON document."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: list[dict[str, str]] | None = None,
        stop_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []
        self.stop_reason = stop_reason


def _validation_diagnostics(error: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "loc": ".".join(str(part) for part in detail["loc"]) or "<root>",
            "type": str(detail["type"]),
        }
        for detail in error.errors()
    ]


def _validate_structured_output(  # noqa: UP047
    response_model: type[ResponseT],
    value: object,
    *,
    stop_reason: str | None = None,
) -> ResponseT:
    try:
        return response_model.model_validate(value)
    except ValidationError as error:
        raise StructuredOutputError(
            "Provider returned schema-invalid structured output",
            diagnostics=_validation_diagnostics(error),
            stop_reason=stop_reason,
        ) from error


def _decode_structured_content(content: str) -> object:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise StructuredOutputError("Provider returned malformed structured output") from error


class AIProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: str,
        response_model: type[ResponseT],
    ) -> ResponseT: ...


class FixtureAIProvider:
    name = "fixture"
    model = "deterministic-v1"

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses

    async def generate(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: str,
        response_model: type[ResponseT],
    ) -> ResponseT:
        del instructions, input_payload
        if agent_name not in self._responses:
            raise RuntimeError(f"Fixture unavailable for {agent_name}")
        return response_model.model_validate(self._responses[agent_name])


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.4-mini",
        timeout_seconds: float = 20,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required for the OpenAI provider")
        self.model = model
        self._timeout = timeout_seconds
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    async def generate(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: str,
        response_model: type[ResponseT],
    ) -> ResponseT:
        response = await asyncio.wait_for(
            self._client.responses.parse(
                model=self.model,
                instructions=instructions,
                input=input_payload,
                text_format=response_model,
                max_output_tokens=1200,
                tools=[],
                tool_choice="none",
                store=False,
                metadata={"agent": agent_name},
            ),
            timeout=self._timeout,
        )
        parsed = cast(ResponseT | None, response.output_parsed)
        if parsed is None:
            raise ValueError(f"{agent_name} returned no structured output")
        return parsed


class OpenRouterProvider:
    """Read-only, schema-constrained OpenRouter adapter with no tool access."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_seconds: float = 20,
        client: Any | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("OpenRouter API key and model are required")
        self.model = model
        self._timeout = timeout_seconds
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=timeout_seconds,
            max_retries=0,
            default_headers={"X-Title": "AlphaDesk"},
        )

    async def generate(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: str,
        response_model: type[ResponseT],
    ) -> ResponseT:
        schema_name = "".join(character for character in agent_name if character.isalnum())[:48]
        for attempt, max_tokens in enumerate((2000, 4000)):
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": instructions + "\nReturn only the required JSON object.",
                        },
                        {"role": "user", "content": input_payload},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name or "AlphaDeskResponse",
                            "strict": True,
                            "schema": response_model.model_json_schema(),
                        },
                    },
                    max_tokens=max_tokens,
                    temperature=0,
                    extra_body={
                        "provider": {"require_parameters": True},
                        "plugins": [{"id": "response-healing"}],
                    },
                ),
                timeout=self._timeout,
            )
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            content = response.choices[0].message.content
            if attempt == 0 and finish_reason in {"length", "max_tokens"}:
                continue
            if not content:
                raise ValueError(f"{agent_name} returned no structured output")
            try:
                return _validate_structured_output(
                    response_model, _decode_structured_content(content)
                )
            except (StructuredOutputError, ValueError):
                if attempt == 0 and finish_reason in {"length", "max_tokens"}:
                    continue
                raise
        raise StructuredOutputError(
            "Provider returned schema-invalid structured output", stop_reason="max_tokens"
        )


class AnthropicProvider:
    """Read-only Anthropic adapter using a forced schema-constrained tool call."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_seconds: float = 20,
        client: Any | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("Anthropic API key and model are required")
        self.model = model
        self._timeout = timeout_seconds
        self._client = client or AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    async def generate(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: str,
        response_model: type[ResponseT],
    ) -> ResponseT:
        tool_name = "".join(character for character in agent_name if character.isalnum())[:48]
        last_schema_error: StructuredOutputError | None = None
        for attempt, max_tokens in enumerate((5000, 8000)):
            retry_instructions = (
                instructions
                if attempt == 0
                else instructions
                + "\nBe concise: summary <= 240 characters, at most 3 limitations, "
                + "at most 5 recommendations, at most 3 risks per recommendation, "
                + "return every required field exactly once, include at least one "
                + "recommendation, and omit as_of if present because the server supplies it."
            )
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=retry_instructions,
                    messages=[{"role": "user", "content": input_payload}],
                    tools=[
                        {
                            "name": tool_name or "AlphaDeskResponse",
                            "description": "Return the required AlphaDesk structured response.",
                            "input_schema": response_model.model_json_schema(),
                        }
                    ],
                    tool_choice={"type": "tool", "name": tool_name or "AlphaDeskResponse"},
                ),
                timeout=self._timeout,
            )
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    try:
                        return _validate_structured_output(
                            response_model,
                            getattr(block, "input", None),
                            stop_reason=getattr(response, "stop_reason", None),
                        )
                    except StructuredOutputError as error:
                        last_schema_error = error
                        if attempt == 0:
                            break
                        raise
            else:
                if attempt == 0 and getattr(response, "stop_reason", None) == "max_tokens":
                    continue
                if last_schema_error is not None:
                    raise last_schema_error
                raise ValueError(f"{agent_name} returned no structured output")
        raise StructuredOutputError(
            "Provider returned schema-invalid structured output",
            stop_reason="max_tokens",
        )
