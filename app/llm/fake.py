"""FakeLLM: a scripted, deterministic test double.

Give it a list of replies; each call to chat()/complete_json() consumes the next one.
A reply may be a value or a function of the call (to compute a reply from the prompt).
Every call is recorded in `.calls` so tests can assert what the app sent.

    fake = FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "paying customers"}),
        FakeLLM.text('{"claims": []}'),
    ])
"""

import itertools
from typing import Any, Callable

from .base import LLMClient, LLMError, LLMResponse, ToolCall, ToolResult, ToolSpec

Reply = LLMResponse | dict[str, Any] | Callable[[dict[str, Any]], Any]


class FakeLLM(LLMClient):
    model = "fake"

    def __init__(self, replies: list[Reply] | None = None, default: Reply | None = None):
        self.replies: list[Reply] = list(replies or [])
        self.default = default  # used for every call once the script is empty
        self.calls: list[dict[str, Any]] = []

    # --- reply builders ---------------------------------------------------

    _ids = itertools.count(1)

    @staticmethod
    def text(text: str) -> LLMResponse:
        return LLMResponse(text=text, stop_reason="end_turn",
                           assistant_message={"role": "assistant", "content": text})

    @classmethod
    def tool_call(cls, name: str, input: dict[str, Any], *more: tuple[str, dict]) -> LLMResponse:
        """One or more tool calls in a single turn: tool_call(name, input, (name2, input2), ...)."""
        calls = [ToolCall(id=f"call_{next(cls._ids)}", name=n, input=i)
                 for n, i in [(name, input), *more]]
        return LLMResponse(
            text="", tool_calls=calls, stop_reason="tool_use",
            assistant_message={"role": "assistant",
                               "tool_calls": [{"id": c.id, "name": c.name, "input": c.input}
                                              for c in calls]},
        )

    # --- LLMClient --------------------------------------------------------

    def chat(self, system: str, messages: list[dict[str, Any]],
             tools: list[ToolSpec] | None = None) -> LLMResponse:
        call = {"method": "chat", "system": system, "messages": list(messages),
                "tools": [t.name for t in tools or []]}
        reply = self._next(call)
        if not isinstance(reply, LLMResponse):
            raise LLMError("FakeLLM: chat() reply must be an LLMResponse")
        return reply

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        call = {"method": "complete_json", "system": system, "prompt": prompt, "schema": schema}
        reply = self._next(call)
        if not isinstance(reply, dict):
            raise LLMError("FakeLLM: complete_json() reply must be a dict")
        return reply

    def tool_results_message(self, results: list[ToolResult], note: str | None = None) -> dict[str, Any]:
        return {"role": "user", "tool_results": [vars(r) for r in results], "note": note}

    def _next(self, call: dict[str, Any]) -> Any:
        self.calls.append(call)
        if self.replies:
            reply = self.replies.pop(0)
        elif self.default is not None:
            reply = self.default
        else:
            raise LLMError(f"FakeLLM: no scripted reply left for call #{len(self.calls)}")
        return reply(call) if callable(reply) else reply
