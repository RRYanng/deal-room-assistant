"""Web search behind a replaceable interface. Use get_web_search(); never call a search API elsewhere.

A provider returns pages as (url, title, content). The research steps store that content and check
every quote against it, so a provider only has to return real text; it is never trusted for facts.
Another provider (e.g. Exa) is one more WebSearchProvider subclass and one more branch below.
"""

from app.config import Settings, get_settings

from .base import WebResult, WebSearchError, WebSearchProvider
from .fake import FakeWebSearch

__all__ = ["get_web_search", "WebResult", "WebSearchError", "WebSearchProvider", "FakeWebSearch"]


def get_web_search(settings: Settings | None = None) -> WebSearchProvider | None:
    """The configured provider, or None when web research is off or has no API key."""
    settings = settings or get_settings()
    if settings.web_search_provider == "tavily":
        if not settings.tavily_api_key:
            return None
        from .tavily import TavilyProvider
        return TavilyProvider(settings.tavily_api_key)
    if settings.web_search_provider == "fake":
        return FakeWebSearch()
    if settings.web_search_provider in ("", "none", "off"):
        return None
    raise ValueError(f"Unknown WEB_SEARCH_PROVIDER: {settings.web_search_provider!r}")
