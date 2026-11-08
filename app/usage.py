"""Counting wrappers for one claim run: model calls, web searches and search credits.

The run wraps its model and search provider in these, then logs the totals with the elapsed time,
so the cost of a check can be read back from the run (GET .../claim-runs/{id} -> usage).
"""

from typing import Any

from app.llm import LLMClient, LLMResponse, ToolResult, ToolSpec
from app.web import WebResult, WebSearchProvider


class CountingLLM(LLMClient):
    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.model = getattr(inner, "model", "unknown")
        self.calls = {"complete_json": 0, "chat": 0}

    def chat(self, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None) -> LLMResponse:
        self.calls["chat"] += 1
        return self.inner.chat(system, messages, tools)

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls["complete_json"] += 1
        return self.inner.complete_json(system, prompt, schema)

    def tool_results_message(self, results: list[ToolResult], note: str | None = None) -> dict[str, Any]:
        return self.inner.tool_results_message(results, note)

    def user_message(self, text: str) -> dict[str, Any]:  # noqa: D102 (the provider's own format)
        return self.inner.user_message(text)


class CountingWeb(WebSearchProvider):
    def __init__(self, inner: WebSearchProvider):
        self.inner = inner
        self.name = getattr(inner, "name", "unknown")
        self.searches = 0
        self._credits_before = getattr(inner, "credits_used", None)

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        self.searches += 1
        return self.inner.search(query, max_results)

    @property
    def credits(self) -> float | None:
        """Credits the provider reported for this run, or None if it does not report them."""
        now = getattr(self.inner, "credits_used", None)
        return None if now is None or self._credits_before is None else now - self._credits_before
