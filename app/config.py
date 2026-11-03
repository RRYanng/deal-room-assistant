"""Runtime configuration, read from environment variables (and an optional .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    llm_provider: str  # "anthropic" or "fake"
    llm_extract_model: str  # metric extraction
    llm_chat_model: str  # Q&A agent and briefs
    llm_effort: str  # "low" | "medium" | "high" | "xhigh" | "max"
    llm_fallbacks: str  # "default" enables server-side refusal fallback; "" disables it
    embedding_model: str
    agent_max_steps: int


def get_settings() -> Settings:
    """Build settings from the current environment. Called at use time so tests can override env vars."""
    db_path = Path(os.getenv("DB_PATH", "data/deal_room.db"))
    if not db_path.is_absolute():  # relative to the project, not to wherever the server was started
        db_path = PROJECT_ROOT / db_path
    return Settings(
        db_path=db_path,
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic").lower(),
        llm_extract_model=os.getenv("LLM_EXTRACT_MODEL", "claude-opus-5-5"),
        llm_chat_model=os.getenv("LLM_CHAT_MODEL", "claude-sonnet-5-5"),
        llm_effort=os.getenv("LLM_EFFORT", "medium"),
        llm_fallbacks=os.getenv("LLM_FALLBACKS", "default"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        agent_max_steps=int(os.getenv("AGENT_MAX_STEPS", "6")),
    )
