"""Provider-neutral web search types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class WebSearchError(Exception):
    """The search call failed. The message is safe to show: it never contains the API key."""


@dataclass
class WebResult:
    url: str
    title: str
    content: str  # page text as returned by the provider


class WebSearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, max_results: int = 5, include_domains: list[str] | None = None) -> list[WebResult]:
        """Pages for `query`, best first, only from `include_domains` when given. Raises WebSearchError."""
