"""Metric extraction validation with a scripted FakeLLM (no network)."""

import pytest

from app.extract import extract_metrics
from app.ingest import ingest_document
from app.llm import FakeLLM, LLMError
from tests.helpers import chunks_in_prompt, fake_extractor


class TinyEmbedder:
    model_name = "tiny"

    def embed(self, texts):
        import numpy as np
        return np.ones((len(texts), 4), dtype=np.float32) / 2


@pytest.fixture
def company(db):
    return db.execute("INSERT INTO companies(name) VALUES ('Tidewell Insurance')").lastrowid


def add(db, company, name, text):
    return ingest_document(db, company, name, "notes", "2026-07-22", text.encode(),
                           embedder=TinyEmbedder())["document_id"]


def stored(db):
    return [dict(r) for r in db.execute("SELECT metric, value, value_raw, unit, currency, as_of_date, quote"
                                        " FROM metrics ORDER BY id")]


def test_prompt_lists_every_chunk_with_its_id(db, company):
    doc = add(db, company, "n.md", "First paragraph.\n\nAs of June 30, 2026, we had 60 paying customers.")
    fake = FakeLLM([{"metrics": []}])
    extract_metrics(db, doc, fake)
    chunks = chunks_in_prompt(fake.calls[0]["prompt"])
    ids = [r["id"] for r in db.execute("SELECT id FROM chunks WHERE document_id = ? ORDER BY chunk_index", (doc,))]
    assert [c[0] for c in chunks] == ids
    assert chunks[1][1] == "n.md ¶2"


def test_valid_metric_is_stored_with_program_normalized_value(db, company):
    doc = add(db, company, "n.md", "As of June 30, 2026, we had sixty paying customers.")
    extract_metrics(db, doc, FakeLLM([fake_extractor([
        {"metric": "paying_customers", "value_raw": "sixty", "as_of_date": "2026-06-30", "unit": "businesses",
         "quote": "As of June 30, 2026, we had sixty paying customers."}])]))
    assert stored(db) == [{"metric": "paying_customers", "value": 60.0, "value_raw": "sixty", "unit": "count",
                           "currency": None, "as_of_date": "2026-06-30",
                           "quote": "As of June 30, 2026, we had sixty paying customers."}]


def test_money_metric_uses_currency_as_unit(db, company):
    doc = add(db, company, "n.md", "We have raised $1.2M in pre-seed funding to date.")
    extract_metrics(db, doc, FakeLLM([fake_extractor([
        {"metric": "funding_raised", "value_raw": "$1.2M", "quote": "We have raised $1.2M in pre-seed funding"}])]))
    m = stored(db)[0]
    assert (m["value"], m["currency"], m["unit"], m["as_of_date"]) == (1_200_000, "USD", "USD", None)


@pytest.mark.parametrize("bad,reason", [
    ({"quote": "We had about 60 paying customers."}, "quote is not verbatim in the chunk"),
    ({"value_raw": "70"}, "value_raw is not part of the quote"),
    ({"chunk_id": 99999}, "chunk_id is not a chunk of this document"),
])
def test_invalid_items_are_dropped_and_logged(db, company, bad, reason):
    doc = add(db, company, "n.md", "As of June 30, 2026, we had 60 paying customers.")
    chunk_id = db.execute("SELECT id FROM chunks WHERE document_id = ?", (doc,)).fetchone()[0]
    item = {"chunk_id": chunk_id, "metric": "paying_customers", "metric_label": None, "value_raw": "60",
            "unit": None, "currency": None, "as_of_date": "2026-06-30",
            "quote": "we had 60 paying customers."} | bad
    result = extract_metrics(db, doc, FakeLLM([{"metrics": [item]}]))
    assert result["kept"] == 0 and result["dropped"][0]["reason"] == reason
    assert stored(db) == []
    log = db.execute("SELECT output_json FROM agent_logs WHERE kind = 'metric_dropped'").fetchone()
    assert reason in log[0]


def test_chunk_of_another_document_is_rejected(db, company):
    other = add(db, company, "other.md", "We had 60 paying customers.")
    doc = add(db, company, "n.md", "Nothing numeric here.")
    other_chunk = db.execute("SELECT id FROM chunks WHERE document_id = ?", (other,)).fetchone()[0]
    item = {"chunk_id": other_chunk, "metric": "paying_customers", "metric_label": None, "value_raw": "60",
            "unit": None, "currency": None, "as_of_date": None, "quote": "We had 60 paying customers."}
    result = extract_metrics(db, doc, FakeLLM([{"metrics": [item]}]))
    assert result["kept"] == 0


def test_as_of_date_not_written_in_chunk_becomes_unclear(db, company):
    doc = add(db, company, "n.md", "We had 60 paying customers.")
    extract_metrics(db, doc, FakeLLM([fake_extractor([
        {"metric": "paying_customers", "value_raw": "60", "as_of_date": "2026-07-22",  # e.g. the document date
         "quote": "We had 60 paying customers."}])]))
    assert stored(db)[0]["as_of_date"] is None


def test_reextraction_replaces_metrics_of_that_document(db, company):
    doc = add(db, company, "n.md", "We had 60 paying customers.")
    rule = [{"metric": "paying_customers", "value_raw": "60", "quote": "We had 60 paying customers."}]
    extract_metrics(db, doc, FakeLLM([fake_extractor(rule)]))
    extract_metrics(db, doc, FakeLLM([fake_extractor(rule)]))
    assert len(stored(db)) == 1


def test_llm_failure_propagates_without_touching_metrics(db, company):
    doc = add(db, company, "n.md", "We had 60 paying customers.")
    with pytest.raises(LLMError):
        extract_metrics(db, doc, FakeLLM())  # empty script -> LLMError
