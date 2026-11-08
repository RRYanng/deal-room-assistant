"""Tavily search (https://tavily.com): returns page text with each result, so quotes can be checked."""

import httpx

from .base import WebResult, WebSearchError, WebSearchProvider

TAVILY_URL = "https://api.tavily.com/search"
MAX_CONTENT_CHARS = 20_000  # per page; enough for the passage that matters, small enough to store


class TavilyProvider(WebSearchProvider):
    name = "tavily"

    def __init__(self, api_key: str, timeout: float = 30.0, client: httpx.Client | None = None):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        try:
            response = self._client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "max_results": max_results, "search_depth": "advanced",
                      "include_raw_content": True},
            )
        except httpx.HTTPError as e:
            raise WebSearchError(f"Tavily request failed ({type(e).__name__}).") from None
        if response.status_code in (401, 403):
            raise WebSearchError("Tavily rejected the API key (check TAVILY_API_KEY).")
        if response.status_code == 429:
            raise WebSearchError("Tavily rate limit or monthly quota reached.")
        if response.status_code != 200:
            raise WebSearchError(f"Tavily returned HTTP {response.status_code}.")
        try:
            results = response.json().get("results") or []
        except ValueError:
            raise WebSearchError("Tavily returned a response that is not JSON.") from None
        pages = []
        for r in results:
            text = (r.get("raw_content") or r.get("content") or "").strip()
            if r.get("url") and text:
                pages.append(WebResult(url=r["url"], title=(r.get("title") or "").strip(),
                                       content=text[:MAX_CONTENT_CHARS]))
        return pages
