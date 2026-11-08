"""Regulatory checklist from config: what the materials mention, never a finding of non-compliance."""

from pathlib import Path

import pytest

from app.claims import create_run, latest_results, run_claim_check
from app.ingest import ingest_document
from app.presentation import build_summary
from app.regulatory import ADDRESSED, NOT_ADDRESSED, check, load_checklists
from tests.helpers import load_samples
from tests.helpers_v2 import DECK, claim_llm, load_clearclaim


def by_id(result):
    return {i["id"]: i for i in result["items"]}


def test_the_checklist_comes_from_the_config_file():
    config = load_checklists()
    terms = [t for item in config["industries"]["healthcare"]["items"] for t in item["terms"]]
    assert "HIPAA" in terms and "Business Associate Agreement" in terms
    code = (Path(__file__).resolve().parent.parent / "app" / "regulatory.py").read_text()
    assert "HIPAA" not in code and "Business Associate" not in code  # nothing industry-specific is hard-coded


def test_clearclaim_is_healthcare_and_does_not_mention_hipaa_or_a_baa(db):
    company_id, _ = load_clearclaim(db, stage=("diligence", "portfolio"))
    result = check(db, company_id)
    assert result["industry"]["id"] == "healthcare"
    items = by_id(result)
    assert items["hipaa"]["status"] == NOT_ADDRESSED and items["baa"]["status"] == NOT_ADDRESSED
    assert items["hipaa"]["question"]["text"].endswith("?")


def test_a_mention_in_any_file_counts_as_addressed_with_its_sentence(db):
    company_id, _ = load_clearclaim(db)
    note = b"SYNTHETIC SAMPLE.\n\nWe sign a Business Associate Agreement with every practice. Data is encrypted."
    ingest_document(db, company_id, "security_notes.md", "notes", "2025-12-10", note)
    baa = by_id(check(db, company_id))["baa"]
    assert baa["status"] == ADDRESSED
    assert baa["quote"] == "We sign a Business Associate Agreement with every practice."
    assert baa["filename"] == "security_notes.md" and baa["question"] is None


def test_the_wording_never_claims_a_breach(db):
    company_id, _ = load_clearclaim(db)
    text = str(check(db, company_id)).lower()
    for word in ("violat", "non-compliant", "noncompliant", "illegal", "breach"):
        assert word not in text


def test_other_industries_are_detected_from_their_files(db):
    ids = load_samples(db)
    assert check(db, ids["Tidewell Insurance"])["industry"]["id"] == "insurance"
    assert check(db, ids["Kitebridge Finance"])["industry"]["id"] == "lending"


def test_a_company_with_too_little_material_gets_no_checklist(db):
    company_id = db.execute("INSERT INTO companies(name) VALUES ('Tiny Co')").lastrowid
    ingest_document(db, company_id, "note.md", "notes", None, b"SYNTHETIC SAMPLE.\n\nWe make a patient app.")
    assert check(db, company_id) == {"industry": None, "items": []}


def test_items_not_addressed_count_in_the_regulatory_summary(db):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), claim_llm())
    out = latest_results(db, company_id)
    missing = [i for i in out["regulatory"]["items"] if i["status"] == NOT_ADDRESSED]
    summary = build_summary(out["claims"], out["changes"], out["regulatory"])
    regulatory = next(c for c in summary["categories"] if c["category"] == "Regulatory")
    assert regulatory["issues"] == len(missing) == 3  # HIPAA, BAA, security assessment
