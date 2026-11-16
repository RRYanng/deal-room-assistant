"""Data checks (conflicts, needs review, changed over time, missing) on the sample data, through the API.
The period rules on ClearClaim are in test_data_checks.py."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.llm import FakeLLM, LLMAuthError
from app.main import app, get_extract_llm
from app.rules import data_checks, display_value, missing_metrics
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
    assert (c["metric"], c["values"]) == ("paying_customers", [60, 80])
    assert c["reason"] == "Dates within 31 days: the same period"  # both as of 2026-06-30
    assert sorted((s["value"], s["loc_label"]) for s in c["sources"]) == [
        (60, "tidewell_meeting_notes.md ¶3"), (80, "tidewell_update_2026-08.txt ¶2")]
    assert all(s["quote"] for s in c["sources"])


def test_a_number_stated_once_is_not_listed_even_without_a_date(client):
    cid = setup_tidewell(client)  # funding $1.2M has no as-of date: it takes the deck's date, nothing to compare
    checks = client.get(f"/api/companies/{cid}/checks").json()
    assert checks["conflicts"] == checks["needs_review"] == checks["changed"] == []
    assert "unclear_basis" not in checks


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
    assert checks_b == {"conflicts": [], "needs_review": [], "changed": [], "missing": ["revenue"]}


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
                raise LLMAuthError("API key invalid or missing. Check .env and restart the server.")
        return NoKey()
    app.dependency_overrides[get_extract_llm] = no_key
    cid = client.post("/api/companies", json={"name": "X"}).json()["id"]
    body = upload(client, cid, "company_a/tidewell_meeting_notes.md", "notes", "2026-07-22")
    assert body["extraction"] == {"status": "failed", "error": "API key invalid or missing. Check .env and restart the server."}
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


def add_document(db, cid, name, doc_date=None):
    did = db.execute("INSERT INTO documents(company_id, filename, doc_type, doc_date) VALUES (?, ?, 'notes', ?)",
                     (cid, name, doc_date)).lastrowid
    chid = db.execute("INSERT INTO chunks(document_id, company_id, chunk_index, text, loc_label)"
                      " VALUES (?, ?, 0, 't', ?)", (did, cid, f"{name} ¶1")).lastrowid
    return did, chid


def test_rule_edge_cases(seeded):
    db, cid, did, chid = seeded  # a.md has no date
    insert_metric(db, cid, did, chid, "paying_customers", 60, "2026-06-30")
    insert_metric(db, cid, did, chid, "paying_customers", 60, "2026-06-30")  # agreement, not conflict
    insert_metric(db, cid, did, chid, "paying_customers", 90, "2026-03-31")  # its own date, 91 days earlier
    insert_metric(db, cid, did, chid, "revenue", 1000, "2026-06-30", "USD", "USD")
    insert_metric(db, cid, did, chid, "revenue", 900, "2026-06-30", "EUR", "EUR")  # different currency
    insert_metric(db, cid, did, chid, "other", 5, "2026-06-30", "%", label="churn")
    insert_metric(db, cid, did, chid, "other", 7, "2026-06-30", "%", label="growth")  # different label
    checks = data_checks(db, cid)
    assert checks["conflicts"] == [] and checks["needs_review"] == []
    assert [c["summary"] for c in checks["changed"]] == ["90 (Mar 2026) → 60 (Jun 2026)"]  # a series in one file
    assert missing_metrics(db, cid) == ["registered_customers", "funding_raised"]

    other, other_chunk = add_document(db, cid, "b.md")  # no date either
    insert_metric(db, cid, other, other_chunk, "paying_customers", 75, None)
    review = data_checks(db, cid)["needs_review"]
    assert [(r["metric"], r["values"]) for r in review] == [("paying_customers", [60, 75, 90])]
    assert "75 (date unknown; b.md, para 1)" in review[0]["summary"]


def test_different_values_in_one_file_conflict_unless_each_states_its_own_date(seeded):
    db, cid, did, chid = seeded
    insert_metric(db, cid, did, chid, "registered_customers", 100, None)
    insert_metric(db, cid, did, chid, "registered_customers", 120, "2026-06-30")  # one of them has no date
    conflicts = data_checks(db, cid)["conflicts"]
    assert [(c["metric"], c["values"], c["reasons"]) for c in conflicts] == [
        ("registered_customers", [100, 120], ["same_file"])]


def test_a_dated_file_gives_its_numbers_its_date(seeded):
    db, cid, _, _ = seeded
    june, june_chunk = add_document(db, cid, "june.md", "2026-06-10")
    july, july_chunk = add_document(db, cid, "july.md", "2026-07-08")  # 28 days later
    insert_metric(db, cid, june, june_chunk, "other", 70, None, "%", label="gross margin")
    insert_metric(db, cid, july, july_chunk, "other", 80, None, "%", label="Gross margin")
    conflict, = data_checks(db, cid)["conflicts"]
    assert [s["date_source"] for s in conflict["sources"]] == ["file", "file"]
    assert conflict["summary"] == "70 (Jun 2026 notes, para 1) vs 80 (Jul 2026 notes, para 1)"


# --- values with units ------------------------------------------------------------

@pytest.mark.parametrize("metric, value, raw, unit, currency, expected", [
    ("other", 4.0, "4", "minutes", None, "4 minutes"),
    ("paying_customers", 60.0, "sixty", "count", None, "60 customers"),
    ("registered_customers", 1250.0, "1,250", "count", None, "1,250 customers"),
    ("funding_raised", 1_200_000.0, "$1.2M", "USD", "USD", "USD 1,200,000"),
    ("revenue", 5_000_000.0, "5M", "unknown currency", None, "5,000,000 (currency not stated)"),
    ("other", 12.5, "12.5%", "%", None, "12.5%"),
    ("other", 3.0, "3", None, None, "3"),
    ("other", 3.0, "3", "count", None, "3"),
    ("other", None, "about 4", "minutes", None, "about 4 minutes"),
    ("funding_raised", None, "$1-2M", "USD", "USD", "$1-2M"),
])
def test_display_value(metric, value, raw, unit, currency, expected):
    assert display_value({"metric": metric, "value": value, "value_raw": raw, "unit": unit,
                          "currency": currency}) == expected
