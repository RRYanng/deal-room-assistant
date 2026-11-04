"""Company brief: program-built sections, verified model sections, sources everywhere."""

import json

import pytest
from fastapi.testclient import TestClient

from app.brief import _quotes_value, brief_to_markdown, generate_brief
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


# --- conflict notes on Traction bullets ------------------------------------------------

RISKS_NOTE = "(conflicting — see Risks)"
LIST_NOTE = "(conflicting — see Conflicting information)"


def brief_with(sections):
    """FakeLLM brief: `sections(metrics)` builds the sections from the get_metrics tool output."""
    def reply(call):
        return FakeLLM.text(json.dumps({"sections": sections(last_tool_output(call)), "open_items": []}))
    return FakeLLM([FakeLLM.tool_call("get_metrics", {}), reply])


def bullet(metrics, name, quote=None):
    m = next(m for m in metrics if m["metric"] == name)
    return {"text": f"About {name}.", "citations": [{"chunk_id": m["chunk_id"], "quote": quote or m["quote"]}]}


def traction_and_risks(metrics):
    return [{"title": "Traction", "items": [bullet(metrics, "paying_customers"),
                                            bullet(metrics, "registered_customers")]},
            {"title": "Risks and gaps", "items": [bullet(metrics, "paying_customers")]}]


def test_traction_bullet_with_conflicting_value_gets_the_note(tidewell):
    conn, cid = tidewell
    brief = generate_brief(conn, cid, "", brief_with(traction_and_risks))
    traction, risks = brief["sections"]
    assert traction["items"][0]["note"] == RISKS_NOTE
    assert "note" not in traction["items"][1]  # registered customers are not in conflict
    assert "note" not in risks["items"][0]  # only Traction bullets are marked
    assert traction["items"][0]["text"] == "About paying_customers."  # the verified text stays as it was


def test_note_appears_after_the_bullet_in_markdown(tidewell):
    conn, cid = tidewell
    md = brief_to_markdown(generate_brief(conn, cid, "", brief_with(traction_and_risks)))
    assert f"- About paying_customers. {RISKS_NOTE} [tidewell_meeting_notes.md ¶3:" in md
    assert "- About registered_customers. [tidewell_deck.pdf" in md


def test_note_points_to_the_conflict_list_when_risks_does_not_cover_it(tidewell):
    conn, cid = tidewell

    def traction_only(metrics):
        return [{"title": "Traction", "items": [bullet(metrics, "paying_customers")]}]

    def risks_elsewhere(metrics):
        return [{"title": "Traction", "items": [bullet(metrics, "paying_customers")]},
                {"title": "Risks", "items": [bullet(metrics, "registered_customers")]}]

    for sections in (traction_only, risks_elsewhere):
        brief = generate_brief(conn, cid, "", brief_with(sections))
        assert brief["sections"][0]["items"][0]["note"] == LIST_NOTE


def test_same_chunk_without_the_conflicting_value_gets_no_note(tidewell):
    conn, cid = tidewell

    def definition_only(metrics):
        return [{"title": "Traction", "items": [
            bullet(metrics, "paying_customers", "meaning businesses with an active paid policy")]}]

    brief = generate_brief(conn, cid, "", brief_with(definition_only))
    assert "note" not in brief["sections"][0]["items"][0]


def test_no_conflict_means_no_note():
    conn = connect(":memory:")
    init_db(conn)
    cid = load_samples(conn)["Tidewell Insurance"]  # without the 4th file there is no conflict

    def traction(metrics):
        return [{"title": "Traction", "items": [bullet(metrics, "paying_customers")]}]

    brief = generate_brief(conn, cid, "", brief_with(traction))
    assert brief["conflicts"] == []
    assert "note" not in brief["sections"][0]["items"][0]
    conn.close()


@pytest.mark.parametrize("quote, raw, expected", [
    ("we had 60 paying customers", "60", True),
    ("we had 60.", "60", True),
    ("We have raised $1.2M to date", "$1.2M", True),
    ("Sixty customers", "sixty", True),
    ("in 2060 we had", "60", False),
    ("60.5 percent", "60", False),
    ("160 customers", "60", False),
])
def test_quotes_value(quote, raw, expected):
    assert _quotes_value(quote, raw) is expected
