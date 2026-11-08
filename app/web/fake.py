"""FakeWebSearch: a scripted, offline test double for WebSearchProvider.

Give it pages per query, or a function of the query. Every query is recorded in `.queries`, so tests
can check what would have been sent to a real search engine.

    web = FakeWebSearch({"dental practices US": [WebResult(url, title, text)]})
"""

from typing import Callable

from .base import WebResult, WebSearchProvider

Pages = dict[str, list[WebResult]] | Callable[[str], list[WebResult]]


class FakeWebSearch(WebSearchProvider):
    name = "fake"

    def __init__(self, pages: Pages | None = None):
        self.pages = pages or {}
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        self.queries.append(query)
        found = self.pages(query) if callable(self.pages) else self.pages.get(query, [])
        return list(found)[:max_results]
