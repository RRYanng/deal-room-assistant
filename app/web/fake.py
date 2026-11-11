"""FakeWebSearch: a scripted, offline test double for WebSearchProvider.

Give it pages per query, or a function of the query. Every query is recorded in `.queries`, and with
its domain list in `.calls`, so tests can check what would have been sent to a real search engine. With
include_domains, only pages on those domains (or their subdomains) are returned, as Tavily does.

    web = FakeWebSearch({"dental practices US": [WebResult(url, title, text)]})
"""

from typing import Callable
from urllib.parse import urlparse

from .base import WebResult, WebSearchProvider

Pages = dict[str, list[WebResult]] | Callable[[str], list[WebResult]]


class FakeWebSearch(WebSearchProvider):
    name = "fake"

    def __init__(self, pages: Pages | None = None):
        self.pages = pages or {}
        self.queries: list[str] = []
        self.calls: list[tuple[str, list[str] | None]] = []

    def search(self, query: str, max_results: int = 5, include_domains: list[str] | None = None) -> list[WebResult]:
        self.queries.append(query)
        self.calls.append((query, include_domains))
        found = self.pages(query) if callable(self.pages) else self.pages.get(query, [])
        if include_domains:  # an empty list limits nothing, as with Tavily
            found = [p for p in found if _on(p.url, include_domains)]
        return list(found)[:max_results]


def _on(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)
