"""Anthropic Claude implementation of LLMClient.

Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth login` profile).
Model, effort and refusal fallback are set in app.config.
"""

import json
from typing import Any

import anthropic

from .base import LLMAuthError, LLMClient, LLMError, LLMResponse, ToolCall, ToolResult, ToolSpec

MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM(LLMClient):
    def __init__(self, model: str, effort: str = "medium", fallbacks: str = "default",
                 client: Any | None = None):
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self._client = client or anthropic.Anthropic()

    # --- public API -------------------------------------------------------

    def chat(self, system: str, messages: list[dict[str, Any]],
             tools: list[ToolSpec] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {"system": system, "messages": messages}
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        return self.to_response(self._create(kwargs))

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = self._create(
            {"system": system, "messages": [self.user_message(prompt)]},
            output_format={"type": "json_schema", "schema": schema},
        )
        parsed = self.to_response(response)
        if parsed.stop_reason != "end_turn":
            raise LLMError(f"JSON call stopped early: {parsed.stop_reason}")
        try:
            return json.loads(parsed.text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Model returned invalid JSON: {e}") from e

    def tool_results_message(self, results: list[ToolResult]) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id,
                 "content": r.content, "is_error": r.is_error}
                for r in results
            ],
        }

    # --- helpers ----------------------------------------------------------

    def _create(self, kwargs: dict[str, Any], output_format: dict[str, Any] | None = None):
        output_config: dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if output_format:
            output_config["format"] = output_format
        if output_config:
            kwargs["output_config"] = output_config
        kwargs.update(model=self.model, max_tokens=MAX_TOKENS)

        try:
            if self.fallbacks:
                return self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks=self.fallbacks, **kwargs)
            return self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMAuthError("API key missing or invalid: check ANTHROPIC_API_KEY in .env.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach Anthropic API: {e}") from e
        except TypeError as e:  # the SDK raises TypeError when no credentials are configured
            if "authentication" in str(e).lower():
                raise LLMAuthError("API key missing: set ANTHROPIC_API_KEY in .env.") from e
            raise

    @staticmethod
    def to_response(response: Any) -> LLMResponse:
        """Convert an SDK Message into the provider-neutral LLMResponse."""
        texts, calls = [], []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        stop = response.stop_reason
        if stop not in ("end_turn", "tool_use", "max_tokens", "refusal"):
            stop = "end_turn"
        return LLMResponse(
            text="".join(texts),
            tool_calls=calls,
            stop_reason=stop,
            # Keep the full content (incl. thinking blocks) so it can be sent back unchanged.
            assistant_message={"role": "assistant", "content": response.content},
        )
