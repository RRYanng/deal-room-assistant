"""Which company a question is about: name matching and POST /api/ask."""

import json

import pytest
from fastapi.testclient import TestClient

from app.llm import FakeLLM, LLMAuthError, LLMError
from app.main import app, get_chat_llm
from app.resolve import find_mentions

TIDEWELL = {"id": 1, "name": "Tidewell Insurance"}
KITEBRIDGE = {"id": 2, "name": "Kitebridge Finance"}
COMPANIES = [TIDEWELL, KITEBRIDGE]


def names(text, companies=COMPANIES):
    return [m["name"] for m in find_mentions(text, companies)]


@pytest.mark.parametrize("text", [
    "What is Tidewell Insurance's revenue?",
    "how many customers does tidewell have",
    "TIDEWELL revenue",
    "What about Tidewel?",  # typo
    "Tide well revenue",
    "tidewell_update_2026-08",
])
def test_name_variants_match(text):
    assert names(text) == ["Tidewell Insurance"]


@pytest.mark.parametrize("text", [
    "How many paying customers does the company have?",
    "Which insurance products are sold?",  # a generic word of the name is not the company
    "What is the finance plan?",
    "Tidewater and bridge loans",  # similar words, different names
    "",
])
def test_other_text_does_not_match(text):
    assert names(text) == []


def test_several_companies_are_all_found_most_mentioned_first():
    assert names("Kitebridge, Kitebridge and Tidewell") == ["Kitebridge Finance", "Tidewell Insurance"]


def test_short_names_need_an_exact_match():
    assert names("Acme revenue", [{"id": 1, "name": "Acme"}]) == ["Acme"]
    assert names("Acne revenue", [{"id": 1, "name": "Acme"}]) == []


def test_name_with_only_generic_words_needs_the_full_name():
    company = [{"id": 1, "name": "Finance Group"}]
    assert names("the finance team", company) == []
    assert names("Finance Group revenue", company) == ["Finance Group"]


def test_companies_sharing_a_name_word_need_the_full_name():
    companies = [{"id": 1, "name": "Tidewell Insurance"}, {"id": 2, "name": "Tidewell Capital"}]
    assert names("Tidewell Capital revenue", companies) == ["Tidewell Capital"]
    assert names("Tidewell revenue", companies) == []


def test_multi_word_name_needs_all_its_words():
    company = [{"id": 1, "name": "Blue Harbor Insurance"}]
    assert names("the blue car", company) == []
    assert names("Blue Harbor revenue", company) == ["Blue Harbor Insurance"]
    assert names("blueharbor_deck", company) == ["Blue Harbor Insurance"]


# --- POST /api/ask ------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def ids(client):
    return {name: client.post("/api/companies", json={"name": name}).json()["id"]
            for name in ("Tidewell Insurance", "Kitebridge Finance")}


def use_fake(reply='{"claims": [], "open_items": ["x: Not found in the provided materials"]}'):
    fake = FakeLLM(default=FakeLLM.text(reply))
    app.dependency_overrides[get_chat_llm] = lambda: fake
    return fake


def test_question_naming_a_company_switches_to_it(client, ids):
    fake = use_fake()
    r = client.post("/api/ask", json={"question": "What did Kitebridge raise?",
                                      "last_company_id": ids["Tidewell Insurance"]}).json()
    assert r["resolution"] == "answered"
    assert r["company"] == {"id": ids["Kitebridge Finance"], "name": "Kitebridge Finance"}
    assert r["company_source"] == "mentioned"
    assert fake.calls[0]["messages"][0]["content"].startswith("Selected company: Kitebridge Finance\n")


def test_question_without_a_name_keeps_the_previous_company(client, ids):
    fake = use_fake()
    r = client.post("/api/ask", json={"question": "And the revenue?",
                                      "last_company_id": ids["Tidewell Insurance"]}).json()
    assert r["company"]["name"] == "Tidewell Insurance"
    assert r["company_source"] == "previous"
    assert fake.calls[0]["messages"][0]["content"].startswith("Selected company: Tidewell Insurance\n")


@pytest.mark.parametrize("last", [None, 99999])
def test_no_name_and_no_valid_previous_company_asks_which(client, ids, last):
    fake = FakeLLM([{"out_of_scope": False}])
    app.dependency_overrides[get_chat_llm] = lambda: fake
    r = client.post("/api/ask", json={"question": "What is the revenue?", "last_company_id": last}).json()
    assert r["resolution"] == "needs_company" and r["reason"] == "unknown"
    assert r["message"] == "Which company do you mean?"
    assert {c["name"] for c in r["companies"]} == set(ids)
    assert [c["method"] for c in fake.calls] == ["complete_json"]  # only the scope check, no agent run


def test_several_companies_in_one_question_asks_to_choose_one(client, ids):
    fake = use_fake()
    r = client.post("/api/ask", json={"question": "Compare Tidewell and Kitebridge",
                                      "last_company_id": ids["Tidewell Insurance"]}).json()
    assert r["resolution"] == "needs_company" and r["reason"] == "multiple"
    assert "one company at a time" in r["message"]
    assert {c["name"] for c in r["companies"]} == set(ids)
    assert fake.calls == []


def test_company_id_is_chosen_by_the_server_not_the_model(client, ids):
    """The model tries to read another company; the tools still run on the resolved one."""
    for name, text in [("Tidewell Insurance", "Tidewell secret plan."), ("Kitebridge Finance", "Kitebridge secret plan.")]:
        files = {"file": (f"{name.split()[0].lower()}.txt", text.encode(), "text/plain")}
        assert client.post(f"/api/companies/{ids[name]}/documents", files=files,
                           data={"doc_type": "notes"}).status_code == 201

    fake = FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "secret plan", "company_id": ids["Kitebridge Finance"]}),
        FakeLLM.text('{"claims": [], "open_items": []}'),
    ])
    app.dependency_overrides[get_chat_llm] = lambda: fake
    r = client.post("/api/ask", json={"question": "What is the Tidewell plan?"}).json()
    found = json.loads(fake.calls[1]["messages"][-1]["tool_results"][0]["content"])
    assert r["company"]["name"] == "Tidewell Insurance"
    assert found and all(row["filename"] == "tidewell.txt" for row in found)


# --- questions unrelated to the materials ---------------------------------------------

OUT_OF_SCOPE = "I can only answer questions about the uploaded company materials."


def test_unrelated_question_with_no_company_gets_a_direct_reply(client, ids):
    fake = FakeLLM([{"out_of_scope": True}])
    app.dependency_overrides[get_chat_llm] = lambda: fake
    r = client.post("/api/ask", json={"question": "What's the weather?"}).json()
    assert r == {"resolution": "out_of_scope", "message": OUT_OF_SCOPE}
    assert [c["method"] for c in fake.calls] == ["complete_json"]
    assert fake.calls[0]["prompt"] == "What's the weather?"


def test_unrelated_question_with_a_previous_company_gets_the_same_reply(client, ids):
    fake = FakeLLM([FakeLLM.text('{"claims": [], "open_items": [], "out_of_scope": true}')])
    app.dependency_overrides[get_chat_llm] = lambda: fake
    r = client.post("/api/ask", json={"question": "What's the weather?",
                                      "last_company_id": ids["Tidewell Insurance"]}).json()
    assert r == {"resolution": "out_of_scope", "message": OUT_OF_SCOPE}


def failing_llm(error):
    def fail(call):
        raise error
    return FakeLLM(default=fail)


@pytest.mark.parametrize("error, status, detail", [
    (LLMAuthError("API key invalid or missing. Check .env and restart the server."), 503,
     "Model call failed: API key invalid or missing. Check .env and restart the server."),
    (LLMError("Could not reach Anthropic API: Connection error."), 502,
     "Model call failed: Could not reach Anthropic API: Connection error."),
    (LLMError("Anthropic API error 500: Internal error"), 502,
     "Model call failed: Anthropic API error 500: Internal error"),
])
def test_failed_scope_check_reports_the_failure(client, ids, error, status, detail):
    """Never "Which company do you mean?": the problem is the model call, not the question."""
    app.dependency_overrides[get_chat_llm] = lambda: failing_llm(error)
    r = client.post("/api/ask", json={"question": "What's the weather?"})
    assert r.status_code == status
    assert r.json() == {"detail": detail}


def test_failed_agent_call_reports_the_failure_the_same_way(client, ids):
    app.dependency_overrides[get_chat_llm] = lambda: failing_llm(LLMAuthError("API key invalid or missing. Check .env and restart the server."))
    r = client.post("/api/ask", json={"question": "And the revenue?", "last_company_id": ids["Tidewell Insurance"]})
    assert r.status_code == 503
    assert r.json()["detail"].startswith("Model call failed: API key invalid or missing.")


def test_named_company_skips_the_scope_check(client, ids):
    fake = use_fake()
    client.post("/api/ask", json={"question": "What did Kitebridge raise?"})
    assert [c["method"] for c in fake.calls] == ["chat"]
