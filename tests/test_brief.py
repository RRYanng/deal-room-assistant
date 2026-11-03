"""Company brief: program-built sections, verified model sections, sources everywhere."""

import json

import pytest
from fastapi.testclient import TestClient

from app.brief import brief_to_markdown, generate_brief
from app.db import connect, init_db
from app.llm import FakeLLM
from app.main import app, get_chat_llm
from tests.helpers import last_tool_output, load_samples


def write_sections(call):
    """FakeLLM final reply after get_metrics: one valid bullet from a metric quote, one fabricated."""
    metrics = last_tool_output(call)
    paying = next(m for m in metrics if m["metric"] == "paying_customers")
    return FakeLLM.text(json.dumps({"sections": [
        {"title": "Traction", "items": [
            {"text": "60 paying customers as of June 30, 2026.",
             "citations": [{"chunk_id": paying["chunk_id"], "quote": paying["quote"]}]},
            {"text": "Revenue grew 3x.", "citations": [{"chunk_id": paying["chunk_id"], "quote": "revenue grew 3x"}]},
        ]},
        {"title": "Risks", "items": [{"text": "Unsupported risk.", "citations": []}]},
    ], "open_items": ["loss ratio: Not found in the provided materials"]}))


def fake_brief_llm():
    return FakeLLM([FakeLLM.tool_call("get_metrics", {}), write_sections])


@pytest.fixture
def tidewell():
    conn = connect(":memory:")
    init_db(conn)
    yield conn, load_samples(conn, include_update=True)["Tidewell Insurance"]
    conn.close()


def test_brief_sections_are_verified(tidewell):
    conn, cid = tidewell
    fake = fake_brief_llm()
    brief = generate_brief(conn, cid, "focus on traction and risks", fake)
    assert "focus on traction and risks" in fake.calls[0]["messages"][0]["content"]
    assert [s["title"] for s in brief["sections"]] == ["Traction"]  # "Risks" had no valid bullet
    assert [i["text"] for i in brief["sections"][0]["items"]] == ["60 paying customers as of June 30, 2026."]
    assert brief["open_items"] == ["loss ratio: Not found in the provided materials"]


def test_program_sections(tidewell):
    conn, cid = tidewell
    brief = generate_brief(conn, cid, "", fake_brief_llm())
    assert brief["requirements"].startswith("General overview")
    assert {(m["metric"], m["value"]) for m in brief["key_metrics"]} == {
        ("registered_customers", 100), ("paying_customers", 60), ("paying_customers", 80)}
    assert [c["date"] for c in brief["recent_communications"]] == ["2026-07-22", "2026-07-15", "2026-06-05"]
    assert brief["missing"] == [{"metric": "revenue", "text": "revenue: Not found in the provided materials"}]
    assert [(c["metric"], c["values"]) for c in brief["conflicts"]] == [("paying_customers", [60, 80])]
    assert [u["metric"] for u in brief["unclear_basis"]] == ["funding_raised"]


def test_every_brief_entry_has_a_source(tidewell):
    conn, cid = tidewell
    brief = generate_brief(conn, cid, "", fake_brief_llm())
    sourced = ([c for s in brief["sections"] for i in s["items"] for c in i["citations"]]
               + brief["key_metrics"] + brief["recent_communications"] + brief["unclear_basis"]
               + [s for c in brief["conflicts"] for s in c["sources"]])
    assert sourced
    for entry in sourced:
        assert entry["loc_label"] and entry["quote"] and entry["chunk_id"]
        text = conn.execute("SELECT text FROM chunks WHERE id = ?", (entry["chunk_id"],)).fetchone()[0]
        assert " ".join(entry["quote"].split()) in " ".join(text.split())


def test_markdown_export_keeps_sources(tidewell):
    conn, cid = tidewell
    md = brief_to_markdown(generate_brief(conn, cid, "", fake_brief_llm()))
    assert "# Company brief: Tidewell Insurance" in md
    assert "- revenue: Not found in the provided materials" in md
    assert '[tidewell_update_2026-08.txt ¶2: "As of June 30, 2026, we had 80 paying customers' in md
    assert "tidewell_crm.csv row 3" in md


def test_brief_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as client:
        cid = client.post("/api/companies", json={"name": "Empty Co"}).json()["id"]
        app.dependency_overrides[get_chat_llm] = lambda: FakeLLM([FakeLLM.text(
            json.dumps({"sections": [], "open_items": ["everything: Not found in the provided materials"]}))])
        r = client.post(f"/api/companies/{cid}/brief", json={"requirements": "focus on risks"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sections"] == [] and len(body["missing"]) == 4
        assert "## Missing information" in body["markdown"]
