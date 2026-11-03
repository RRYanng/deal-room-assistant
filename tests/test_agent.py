"""Q&A agent loop with a scripted FakeLLM: tools, company injection, step limit, citation checks."""

import json

import pytest
from fastapi.testclient import TestClient

from app.agent import answer_question, parse_json_object, run_tool
from app.db import connect, init_db
from app.llm import FakeLLM, LLMAuthError
from app.logs import get_run_log
from app.main import app, get_chat_llm
from tests.helpers import cite_from_search, last_tool_output, load_samples


@pytest.fixture(scope="module")
def sample():
    conn = connect(":memory:")
    init_db(conn)
    ids = load_samples(conn)
    yield conn, ids["Tidewell Insurance"], ids["Kitebridge Finance"]
    conn.close()


def chunk_id_of(conn, needle):
    return conn.execute("SELECT id FROM chunks WHERE text LIKE ?", (f"%{needle}%",)).fetchone()[0]


# --- tools ------------------------------------------------------------------------

def test_tools_return_only_the_selected_company(sample):
    conn, a, b = sample
    for tool, args in [("search_documents", {"query": "paying customers"}), ("get_metrics", {}),
                       ("get_crm_history", {})]:
        out = run_tool(conn, a, tool, args)
        assert out and all(r["filename"].startswith("tidewell") if "filename" in r else True for r in out)
        assert all(not str(r.get("loc_label", "")).startswith("kitebridge") for r in out)


def test_metrics_tool_includes_chunk_id_and_quote(sample):
    conn, a, _ = sample
    paying = run_tool(conn, a, "get_metrics", {"metric": "paying_customers"})
    assert [(m["value"], m["chunk_id"] > 0, bool(m["quote"])) for m in paying] == [(60, True, True)]


def test_crm_history_is_sorted_by_date(sample):
    conn, a, _ = sample
    dates = [r["date"] for r in run_tool(conn, a, "get_crm_history", {})]
    assert dates == sorted(dates) == ["2026-06-05", "2026-07-15", "2026-07-22"]


# --- loop -------------------------------------------------------------------------

def test_full_loop_with_valid_citation(sample):
    conn, a, _ = sample
    fake = FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "paying customers"}),
        cite_from_search("Tidewell had 60 paying customers", "Tidewell had 60 paying customers as of June 30, 2026."),
    ])
    result = answer_question(conn, a, "How many paying customers?", fake)
    assert result["status"] == "ok"
    claim = result["claims"][0]
    assert claim["citations"][0]["loc_label"] == "tidewell_meeting_notes.md ¶3"
    assert "60 paying customers" in claim["citations"][0]["chunk_text"]
    assert fake.calls[0]["tools"] == ["search_documents", "get_metrics", "get_conflicts", "get_crm_history"]


def test_company_id_from_model_is_ignored(sample):
    conn, a, b = sample
    fake = FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "paying customers", "company_id": b}),
        lambda call: FakeLLM.text(json.dumps({"claims": [], "open_items": [
            "files: " + ",".join(sorted({r["filename"] for r in last_tool_output(call)}))]})),
    ])
    result = answer_question(conn, a, "q", fake)
    assert "kitebridge" not in result["open_items"][0] and "tidewell" in result["open_items"][0]
    kinds = [e["kind"] for e in get_run_log(conn, result["run_id"])]
    assert "company_id_ignored" in kinds


def test_step_limit(sample, monkeypatch):
    conn, a, _ = sample
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    fake = FakeLLM(default=FakeLLM.tool_call("search_documents", {"query": "x"}))
    result = answer_question(conn, a, "q", fake)
    assert result["status"] == "step_limit" and len(fake.calls) == 6
    assert "Step limit reached" in result["open_items"][0]
    # before the last step the model is told to stop calling tools
    assert fake.calls[5]["messages"][-1]["note"].startswith("You have one step left")
    assert fake.calls[4]["messages"][-1]["note"] is None


def test_fabricated_quote_is_dropped_and_logged(sample):
    conn, a, _ = sample
    good = chunk_id_of(conn, "Tidewell had 60 paying customers")
    final = {"claims": [
        {"text": "60 paying customers.", "citations": [
            {"chunk_id": good, "quote": "Tidewell had 60 paying customers"},
            {"chunk_id": good, "quote": "Tidewell had 600 paying customers"}]},
        {"text": "Revenue is $2M.", "citations": [{"chunk_id": good, "quote": "revenue of $2M"}]},
    ], "open_items": []}
    result = answer_question(conn, a, "q", FakeLLM([FakeLLM.text(json.dumps(final))]))
    assert [c["text"] for c in result["claims"]] == ["60 paying customers."]
    assert len(result["claims"][0]["citations"]) == 1
    kinds = [e["kind"] for e in get_run_log(conn, result["run_id"])]
    assert kinds.count("citation_dropped") == 2 and "claim_dropped" in kinds


def test_citation_to_other_company_chunk_is_dropped(sample):
    conn, a, _ = sample
    other = chunk_id_of(conn, "410 paying customers")
    final = {"claims": [{"text": "410.", "citations": [
        {"chunk_id": other, "quote": "Kitebridge had 410 paying customers"}]}], "open_items": []}
    result = answer_question(conn, a, "q", FakeLLM([FakeLLM.text(json.dumps(final))]))
    assert result["claims"] == []
    assert result["open_items"] == [f"q: Not found in the provided materials"]


def test_not_found_open_item_passes_through(sample):
    conn, a, _ = sample
    final = {"claims": [], "open_items": ["revenue: Not found in the provided materials"]}
    result = answer_question(conn, a, "What is the revenue?", FakeLLM([FakeLLM.text(json.dumps(final))]))
    assert result["open_items"] == ["revenue: Not found in the provided materials"]


def test_conflict_attached_when_cited_chunk_is_part_of_it():
    conn = connect(":memory:")
    init_db(conn)
    a = load_samples(conn, include_update=True)["Tidewell Insurance"]
    fake = FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "paying customers"}),
        cite_from_search("Tidewell had 60 paying customers", "60 paying customers per the meeting notes."),
    ])
    result = answer_question(conn, a, "How many paying customers?", fake)
    assert [c["values"] for c in result["conflicts"]] == [[60, 80]]


def test_unparseable_answer_is_retried_once(sample):
    conn, a, _ = sample
    fake = FakeLLM([FakeLLM.text("Sure! Tidewell has customers."),
                    FakeLLM.text('```json\n{"claims": [], "open_items": ["x: Not found in the provided materials"]}\n```')])
    result = answer_question(conn, a, "q", fake)
    assert result["status"] == "ok" and result["open_items"] == ["x: Not found in the provided materials"]
    assert fake.calls[1]["messages"][-1]["content"].startswith("Reply with only the JSON")


def test_tool_error_is_returned_to_model(sample):
    conn, a, _ = sample
    fake = FakeLLM([FakeLLM.tool_call("search_documents", {"query": ""}),
                    lambda call: FakeLLM.text(json.dumps({"claims": [], "open_items": [
                        "error seen: " + str(call["messages"][-1]["tool_results"][0]["is_error"])]}))])
    assert answer_question(conn, a, "q", fake)["open_items"] == ["error seen: True"]


def test_parse_json_object():
    assert parse_json_object('Here: {"a": 1} done') == {"a": 1}
    assert parse_json_object("no json") is None


# --- API ------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as c:
        yield c


def test_ask_endpoint_and_run_log(client):
    cid = client.post("/api/companies", json={"name": "Tidewell Insurance"}).json()["id"]
    client.post(f"/api/companies/{cid}/documents", data={"doc_type": "notes"},
                files={"file": ("n.md", b"As of June 30, 2026, Tidewell had 60 paying customers.")})
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "paying"}),
        cite_from_search("Tidewell had 60 paying customers", "60 paying customers."),
    ])
    r = client.post(f"/api/companies/{cid}/ask", json={"question": "How many paying customers?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claims"][0]["citations"][0]["loc_label"] == "n.md ¶1"
    log = client.get(f"/api/runs/{body['run_id']}/log").json()
    assert [e["kind"] for e in log if e["kind"] == "tool_call"] == ["tool_call"]
    assert log[1]["tool"] == "search_documents" and log[1]["input"] == {"query": "paying"}


def test_ask_without_api_key_says_api_key_missing(client):
    cid = client.post("/api/companies", json={"name": "X"}).json()["id"]

    class NoKey(FakeLLM):
        def chat(self, *a, **k):
            raise LLMAuthError("API key missing: set ANTHROPIC_API_KEY in .env.")
    app.dependency_overrides[get_chat_llm] = lambda: NoKey()
    r = client.post(f"/api/companies/{cid}/ask", json={"question": "q"})
    assert r.status_code == 503 and r.json()["detail"].startswith("API key missing")
