"""Unified LLM entry point. Use get_llm(); never import a provider SDK elsewhere."""

from app.config import Settings, get_settings

from .base import LLMAuthError, LLMClient, LLMError, LLMResponse, ToolCall, ToolResult, ToolSpec
from .fake import FakeLLM

__all__ = ["get_llm", "LLMAuthError", "LLMClient", "LLMError", "LLMResponse", "ToolCall", "ToolResult",
           "ToolSpec", "FakeLLM"]


def get_llm(purpose: str = "chat", settings: Settings | None = None) -> LLMClient:
    """purpose: "extract" (metric extraction) or "chat" (Q&A agent and briefs)."""
    settings = settings or get_settings()
    if purpose not in ("extract", "chat"):
        raise ValueError(f"Unknown LLM purpose: {purpose!r}")
    if settings.llm_provider == "anthropic":
        from .anthropic_client import AnthropicLLM
        model = settings.llm_extract_model if purpose == "extract" else settings.llm_chat_model
        return AnthropicLLM(model, settings.llm_effort, settings.llm_fallbacks, cache_chat=settings.llm_prompt_cache)
    if settings.llm_provider == "fake":
        return FakeLLM()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
