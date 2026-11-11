import pytest

from app.db import connect, init_db
from app.llm import FakeLLM
from app.main import app, get_chat_llm, get_extract_llm, get_web


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def fake_llms():
    """API tests never hit a real model or web search: extraction finds nothing, chat has no script,
    web research is off. Tests that need specific behaviour override these dependencies themselves."""
    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default={"metrics": []})
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM()
    app.dependency_overrides[get_web] = lambda: None
    yield
    app.dependency_overrides.clear()
