"""Conflicts, unclear basis and missing metrics on the sample data, through the API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.llm import FakeLLM, LLMAuthError
from app.main import app, get_extract_llm
from app.rules import find_conflicts, missing_metrics
from tests.helpers import KITEBRIDGE_RULES, TIDEWELL_RULES, fake_extractor

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(
        default=fake_extractor(TIDEWELL_RULES + KITEBRIDGE_RULES))
    with TestClient(app) as c:
        yield c


def upload(client, cid, rel, doc_type, doc_date):
    path = SAMPLES / rel
    r = client.post(f"/api/companies/{cid}/documents", files={"file": (path.name, path.read_bytes())},
                    data={"doc_type": doc_type, "doc_date": doc_date})
    assert r.status_code == 201, r.text
    return r.json()


def setup_tidewell(client):
    cid = client.post("/api/companies", json={"name": "Tidewell Insurance"}).json()["id"]
    upload(client, cid, "company_a/tidewell_deck.pdf", "deck", "2026-07-15")
    upload(client, cid, "company_a/tidewell_meeting_notes.md", "notes", "2026-07-22")
    upload(client, cid, "company_a/tidewell_crm.csv", "crm", "2026-07-22")
    return cid


def test_registered_100_vs_paying_60_is_not_a_conflict(client):
    cid = setup_tidewell(client)
    metrics = {(m["metric"], m["value"]) for m in client.get(f"/api/companies/{cid}/metrics").json()}
    assert {("registered_customers", 100), ("paying_customers", 60)} <= metrics
    assert client.get(f"/api/companies/{cid}/checks").json()["conflicts"] == []


def test_fourth_file_creates_paying_conflict_with_both_sources(client):
    cid = setup_tidewell(client)
    upload(client, cid, "company_a/tidewell_update_2026-08.txt", "notes", "2026-08-10")
    conflicts = client.get(f"/api/companies/{cid}/checks").json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert (c["metric"], c["as_of_date"], c["values"]) == ("paying_customers", "2026-06-30", [60, 80])
    assert sorted((s["value"], s["loc_label"]) for s in c["sources"]) == [
        (60, "tidewell_meeting_notes.md ¶3"), (80, "tidewell_update_2026-08.txt ¶2")]
    assert all(s["quote"] for s in c["sources"])


def test_funding_without_as_of_date_is_unclear_basis(client):
    cid = setup_tidewell(client)
    unclear = client.get(f"/api/companies/{cid}/checks").json()["unclear_basis"]
    assert [(u["metric"], u["value"], u["reason"], u["loc_label"]) for u in unclear] == [
        ("funding_raised", 1_200_000, "no as-of date stated", "tidewell_deck.pdf p.4 ¶2")]


def test_revenue_is_listed_as_missing(client):
    cid = setup_tidewell(client)
    assert client.get(f"/api/companies/{cid}/checks").json()["missing"] == ["revenue"]


def test_other_company_metrics_do_not_mix(client):
    a = setup_tidewell(client)
    b = client.post("/api/companies", json={"name": "Kitebridge Finance"}).json()["id"]
    upload(client, b, "company_b/kitebridge_deck.pdf", "deck", "2026-04-10")
    upload(client, b, "company_b/kitebridge_meeting_notes.md", "notes", "2026-04-20")
    a_files = {m["filename"] for m in client.get(f"/api/companies/{a}/metrics").json()}
    b_metrics = client.get(f"/api/companies/{b}/metrics").json()
    assert all(f.startswith("tidewell") for f in a_files)
    assert {(m["metric"], m["value"]) for m in b_metrics} == {
        ("registered_customers", 1250), ("funding_raised", 8_500_000), ("paying_customers", 410)}
    checks_b = client.get(f"/api/companies/{b}/checks").json()
    assert checks_b == {"conflicts": [], "unclear_basis": [], "missing": ["revenue"]}


def test_replacing_file_removes_its_conflict(client):
    cid = setup_tidewell(client)
    upload(client, cid, "company_a/tidewell_update_2026-08.txt", "notes", "2026-08-10")
    r = client.post(f"/api/companies/{cid}/documents",
                    files={"file": ("tidewell_update_2026-08.txt", b"Corrected update: no numbers here.")},
                    data={"doc_type": "notes"})
    assert r.json()["replaced"] is True
    assert client.get(f"/api/companies/{cid}/checks").json()["conflicts"] == []


def test_upload_survives_extraction_failure(client):
    def no_key():
        class NoKey(FakeLLM):
            def complete_json(self, *a, **k):
                raise LLMAuthError("API key missing: set ANTHROPIC_API_KEY in .env.")
        return NoKey()
    app.dependency_overrides[get_extract_llm] = no_key
    cid = client.post("/api/companies", json={"name": "X"}).json()["id"]
    body = upload(client, cid, "company_a/tidewell_meeting_notes.md", "notes", "2026-07-22")
    assert body["extraction"] == {"status": "failed", "error": "API key missing: set ANTHROPIC_API_KEY in .env."}
    assert len(client.get(f"/api/companies/{cid}/documents").json()) == 1


# --- rule edge cases directly on the table ----------------------------------------

def insert_metric(db, cid, did, chid, metric, value, as_of, unit="count", currency=None, label=None):
    db.execute("INSERT INTO metrics(company_id, document_id, chunk_id, metric, metric_label, value, value_raw,"
               " unit, currency, as_of_date, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'q')",
               (cid, did, chid, metric, label, value, str(value), unit, currency, as_of))


@pytest.fixture
def seeded(db):
    cid = db.execute("INSERT INTO companies(name) VALUES ('X')").lastrowid
    did = db.execute("INSERT INTO documents(company_id, filename, doc_type) VALUES (?, 'a.md', 'notes')",
                     (cid,)).lastrowid
    chid = db.execute("INSERT INTO chunks(document_id, company_id, chunk_index, text, loc_label)"
                      " VALUES (?, ?, 0, 't', 'a.md ¶1')", (did, cid)).lastrowid
    return db, cid, did, chid


def test_rule_edge_cases(seeded):
    db, cid, did, chid = seeded
    insert_metric(db, cid, did, chid, "paying_customers", 60, "2026-06-30")
    insert_metric(db, cid, did, chid, "paying_customers", 60, "2026-06-30")  # agreement, not conflict
    insert_metric(db, cid, did, chid, "paying_customers", 90, "2026-03-31")  # different date
    insert_metric(db, cid, did, chid, "paying_customers", 75, None)  # unclear basis: never compared
    insert_metric(db, cid, did, chid, "revenue", 1000, "2026-06-30", "USD", "USD")
    insert_metric(db, cid, did, chid, "revenue", 900, "2026-06-30", "EUR", "EUR")  # different currency
    insert_metric(db, cid, did, chid, "other", 5, "2026-06-30", "%", label="churn")
    insert_metric(db, cid, did, chid, "other", 7, "2026-06-30", "%", label="growth")  # different label
    assert find_conflicts(db, cid) == []
    assert missing_metrics(db, cid) == ["registered_customers", "funding_raised"]
