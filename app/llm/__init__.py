"""Unified LLM entry point. Use get_llm(); never import a provider SDK elsewhere."""

from app.config import Settings, get_settings

from .base import LLMClient, LLMError, LLMResponse, ToolCall, ToolResult, ToolSpec
from .fake import FakeLLM

__all__ = ["get_llm", "LLMClient", "LLMError", "LLMResponse", "ToolCall", "ToolResult",
           "ToolSpec", "FakeLLM"]


def get_llm(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    if settings.llm_provider == "anthropic":
        from .anthropic_client import AnthropicLLM
        return AnthropicLLM(settings.llm_model, settings.llm_effort, settings.llm_fallbacks)
    if settings.llm_provider == "fake":
        return FakeLLM()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
