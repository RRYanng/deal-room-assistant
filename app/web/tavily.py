"""Tavily search (https://tavily.com): returns page text with each result, so quotes can be checked.

Request and response follow the API reference (docs.tavily.com, POST /search), checked 2026-11-08:
Bearer authentication; `search_depth: "basic"` costs 1 credit per request ("advanced" costs 2);
`include_raw_content: "text"` returns the page text; `include_usage` reports the credits used;
`include_domains` (at most 300) limits results to those domains.
"""

import httpx

from .base import WebResult, WebSearchError, WebSearchProvider

TAVILY_URL = "https://api.tavily.com/search"
MAX_CONTENT_CHARS = 20_000  # per page; enough for the passage that matters, small enough to store
ERRORS = {
    400: "Tavily refused the request as invalid.",
    401: "Tavily rejected the API key (check TAVILY_API_KEY).",
    422: "Tavily refused the request parameters.",
    429: "Tavily rate limit reached; try again shortly.",
    432: "Tavily plan or key limit reached (the free plan allows 1,000 credits a month).",
    433: "Tavily pay-as-you-go limit reached.",
    434: "Tavily is over capacity; try again later.",
}


class TavilyProvider(WebSearchProvider):
    name = "tavily"

    def __init__(self, api_key: str, timeout: float = 30.0, client: httpx.Client | None = None):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)
        self.credits_used = 0.0  # as reported by Tavily, summed over this provider's calls

    def search(self, query: str, max_results: int = 5, include_domains: list[str] | None = None) -> list[WebResult]:
        body = {"query": query, "max_results": max_results, "search_depth": "basic",
                "include_raw_content": "text", "include_usage": True}
        if include_domains:
            body["include_domains"] = list(include_domains)
        try:
            response = self._client.post(TAVILY_URL, headers={"Authorization": f"Bearer {self._api_key}"}, json=body)
        except httpx.HTTPError as e:
            raise WebSearchError(f"Tavily request failed ({type(e).__name__}).") from None
        if response.status_code != 200:
            raise WebSearchError(ERRORS.get(response.status_code, f"Tavily returned HTTP {response.status_code}."))
        try:
            body = response.json()
        except ValueError:
            raise WebSearchError("Tavily returned a response that is not JSON.") from None
        usage = body.get("usage") or {}
        if isinstance(usage.get("credits"), (int, float)):
            self.credits_used += usage["credits"]
        pages = []
        for r in body.get("results") or []:
            text = (r.get("raw_content") or r.get("content") or "").strip()
            if r.get("url") and text:
                pages.append(WebResult(url=r["url"], title=(r.get("title") or "").strip(),
                                       content=text[:MAX_CONTENT_CHARS]))
        return pages
