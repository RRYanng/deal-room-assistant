"""Calls the real model. Run with: pytest -m live  (needs Anthropic credentials)."""

import pytest

from app.llm import get_llm

pytestmark = pytest.mark.live


def test_real_model_returns_json():
    llm = get_llm()
    out = llm.complete_json(
        "You extract numbers.",
        "SYNTHETIC SAMPLE: We had 60 paying customers.",
        {"type": "object", "properties": {"paying_customers": {"type": "integer"}},
         "required": ["paying_customers"], "additionalProperties": False},
    )
    assert out == {"paying_customers": 60}


def test_real_extraction_on_tidewell_samples(db):
    """Verification 1-3 with the real extraction model: registered != paying, conflict kept, unclear basis."""
    from pathlib import Path

    from app.extract import extract_metrics
    from app.ingest import ingest_document
    from app.rules import company_checks, get_metrics

    samples = Path(__file__).resolve().parent.parent / "samples" / "company_a"
    cid = db.execute("INSERT INTO companies(name) VALUES ('Tidewell Insurance')").lastrowid
    llm = get_llm("extract")
    for name, doc_type, doc_date in [("tidewell_deck.pdf", "deck", "2026-07-15"),
                                     ("tidewell_meeting_notes.md", "notes", "2026-07-22"),
                                     ("tidewell_update_2026-08.txt", "notes", "2026-08-10")]:
        doc = ingest_document(db, cid, name, doc_type, doc_date, (samples / name).read_bytes())
        result = extract_metrics(db, doc["document_id"], llm)
        print(name, {k: v for k, v in result.items() if k != "dropped"}, result["dropped"])

    metrics = get_metrics(db, cid)
    for m in metrics:
        print(m["metric"], m["value"], m["as_of_date"], m["loc_label"], "|", m["quote"])
    pairs = {(m["metric"], m["value"], m["as_of_date"]) for m in metrics}
    assert ("registered_customers", 100, "2026-06-30") in pairs
    assert ("paying_customers", 60, "2026-06-30") in pairs
    assert ("paying_customers", 80, "2026-06-30") in pairs
    assert not any(m["metric"] == "paying_customers" and m["value"] == 100 for m in metrics)
    assert not any(m["metric"] == "revenue" for m in metrics)

    checks = company_checks(db, cid)
    print(checks)
    paying = [c for c in checks["conflicts"] if c["metric"] == "paying_customers"]
    assert paying and paying[0]["values"] == [60, 80]
    assert "revenue" in checks["missing"]
    assert any(u["metric"] == "funding_raised" and u["value"] == 1_200_000 for u in checks["unclear_basis"])


def _ask_live(db, question, include_update=False):
    from app.agent import answer_question
    from tests.helpers import load_samples

    cid = load_samples(db, include_update=include_update)["Tidewell Insurance"]
    result = answer_question(db, cid, question, get_llm("chat"))
    print(f"\nQ: {question}\nstatus={result['status']} tools={[t['tool'] for t in result['tool_calls']]}")
    for c in result["claims"]:
        print("  CLAIM:", c["text"], "|", [x["loc_label"] for x in c["citations"]])
    print("  OPEN:", result["open_items"])
    print("  CONFLICTS:", [(c["metric"], c["values"]) for c in result["conflicts"]])
    return result


def test_real_agent_paying_customers(db):
    r = _ask_live(db, "How many paying customers does the company have?")
    labels = {x["loc_label"] for c in r["claims"] for x in c["citations"]}
    assert "tidewell_meeting_notes.md ¶3" in labels
    assert any("60" in c["text"] for c in r["claims"])
    assert not any("100" in c["text"] and "paying" in c["text"].lower() for c in r["claims"])


def test_real_agent_last_conversation(db):
    r = _ask_live(db, "When was our last conversation with the company, and what are the next steps?")
    labels = {x["loc_label"] for c in r["claims"] for x in c["citations"]}
    assert "tidewell_crm.csv row 3" in labels


def test_real_agent_revenue_missing(db):
    r = _ask_live(db, "What is the company's revenue?")
    assert r["status"] == "ok"
    assert any("not found in the provided materials" in o.lower() for o in r["open_items"])
    assert not any("$" in c["text"] and "revenue" in c["text"].lower() for c in r["claims"])


def test_real_agent_conflict_after_fourth_file(db):
    r = _ask_live(db, "How many paying customers does the company have?", include_update=True)
    text = " ".join(c["text"] for c in r["claims"])
    assert "60" in text and "80" in text
    assert [c["values"] for c in r["conflicts"]] == [[60, 80]]
