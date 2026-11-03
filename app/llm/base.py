"""Provider-neutral LLM interface.

The rest of the app only uses these types. Each provider (Anthropic, Fake, ...)
implements LLMClient. Conversation messages are kept in the provider's own format:
callers append `response.assistant_message` and `client.tool_results_message(...)`
verbatim, so nothing provider-specific (e.g. thinking blocks) is lost between turns.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Raised when the model call fails or returns something unusable."""


class LLMAuthError(LLMError):
    """No usable credentials (e.g. ANTHROPIC_API_KEY is empty)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str  # JSON-encoded tool output
    is_error: bool = False


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens" | "refusal"
    assistant_message: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    model: str

    @abstractmethod
    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        """One model turn. May return tool calls (stop_reason == "tool_use") or final text."""

    @abstractmethod
    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Single call whose output must be a JSON object matching `schema`."""

    @abstractmethod
    def tool_results_message(self, results: list[ToolResult]) -> dict[str, Any]:
        """Build the next user message carrying tool results, in this provider's format."""

    @staticmethod
    def user_message(text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}
