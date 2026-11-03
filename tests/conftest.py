import pytest

from app.db import connect, init_db
from app.llm import FakeLLM
from app.main import app, get_chat_llm, get_extract_llm


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def fake_llms():
    """API tests never hit a real model: extraction finds nothing, chat has no script.
    Tests that need specific model behaviour override these dependencies themselves."""
    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default={"metrics": []})
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM()
    yield
    app.dependency_overrides.clear()
