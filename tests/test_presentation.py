"""How results read: plain labels, one-line conclusions, neutral sources first, and the summary."""

import sqlite3

import pytest

from app.claims import KEYS, create_run, latest_results, run_claim_check
from app.db import SCHEMA, connect, init_db
from app.memo import build_memo, memo_to_markdown
from app.presentation import KEY_LABELS, build_summary
from app.reviews import ReviewError, apply_reviews, record_review
from tests.helpers_v2 import DECK, by_quote, fake_web, load_clearclaim, research_llm


@pytest.fixture
def checked(db):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    out = latest_results(db, company_id)
    apply_reviews(db, company_id, out["claims"])
    apply_reviews(db, company_id, out["changes"], "drift")
    return company_id, out


def test_every_key_has_a_plain_label(checked):
    assert set(KEY_LABELS) == set(KEYS)
    assert all("." not in c["label"] for c in checked[1]["claims"])


def test_evidence_has_a_one_line_conclusion(checked):
    claims = checked[1]["claims"]
    practices = by_quote(claims, "400,000 independent dental practices")
    assert [e["headline"] for e in practices["evidence"]] == ["ADA: 205,088 (upper bound); deck says 400,000"]
    size = next(c for c in claims if c["key"] == "market.size_usd")
    assert size["evidence"][0]["headline"] == "400,000 x $3,600 = $1,440,000,000, not $12B"
    margin = by_quote(claims, "Current gross margin: above 90%")
    heads_margin = [e["headline"] for e in margin["evidence"]]
    assert "Meeting notes: around 70%; deck says above 90%" in heads_margin
    assert "Deck says above 90% here and 67-82% on p.5 ¶2" in heads_margin
    heads = by_quote(claims, "team of 14")
    assert heads["evidence"][0]["headline"] == "$420,000 / 14 = $30,000 per full-time employee, below the $48,651 floor"
    competition = by_quote(claims, "No other company offers")
    assert competition["evidence"][0]["headline"] == "2 competitors found: Molarflow, Toothledger"


def test_neutral_sources_come_first():
    from app.presentation import decorate
    claim = {"key": "market.customer_count", "value_raw": "400,000", "filename": "deck.pdf", "evidence": [
        {"source": "external", "source_type": "other", "stance": "contradicts", "value_raw": "135,665",
         "note": "x", "url": "https://vendor.example.com", "title": "Vendor"},
        {"source": "external", "source_type": "government/association", "stance": "contradicts",
         "value_raw": "205,088", "note": "an upper bound", "url": "https://www.ada.org/x", "title": "ADA"}]}
    assert [e["source_type"] for e in decorate(claim)["evidence"]] == ["government/association", "other"]


def test_summary_counts_issues_by_category_and_names_the_top_three(checked):
    summary = build_summary(checked[1]["claims"], checked[1]["changes"])
    counts = {c["category"]: c["issues"] for c in summary["categories"]}
    # Market: practices, market size, industry spending + 2 changes since the September deck.
    assert counts == {"Market": 5, "Financials": 4, "Competition": 1, "Team": 1, "Regulatory": 0}
    assert len(summary["top"]) == 3
    assert summary["top"][0]["text"] == "Number of target customers: ADA: 205,088 (upper bound); deck says 400,000."
    assert len({t["text"].split(":")[0] for t in summary["top"]}) == 3  # one line per subject


def test_dismissed_items_leave_the_summary(db, checked):
    company_id, out = checked
    practices = by_quote(out["claims"], "400,000 independent dental practices")
    record_review(db, company_id, practices["fingerprint"], "dismissed", "Known; discussed with the founder.")
    again = latest_results(db, company_id)
    apply_reviews(db, company_id, again["claims"])
    summary = build_summary(again["claims"], again["changes"])
    assert all("Number of target customers: ADA" not in t["text"] for t in summary["top"])


# --- review rules -----------------------------------------------------------------------------

def test_confirm_needs_no_reason_and_dismiss_does(db, checked):
    company_id, out = checked
    fp = out["claims"][0]["fingerprint"]
    record_review(db, company_id, fp, "confirmed", "")
    with pytest.raises(ReviewError, match="reason is required to dismiss"):
        record_review(db, company_id, fp, "dismissed", "  ")
    with pytest.raises(sqlite3.IntegrityError):  # the database holds the same rule
        db.execute("INSERT INTO reviews(company_id, item_type, fingerprint, decision, reason)"
                   " VALUES (?, 'claim', 'fp', 'dismissed', NULL)", (company_id,))


def test_a_change_since_the_last_deck_can_be_dismissed(db, checked):
    company_id, out = checked
    change = next(c for c in out["changes"] if c["key"] == "market.customer_count")
    record_review(db, company_id, change["fingerprint"], "dismissed", "Definition changed to all practices.",
                  item_type="drift")
    memo = build_memo(db, company_id)
    assert next(c for c in memo["changes"] if c["key"] == "market.customer_count")["review_state"] == "dismissed"
    assert "250,000" not in memo_to_markdown(memo).split("## Changed since the last deck")[1].split("##")[0]


def test_confirmed_finding_without_reason_reads_cleanly_in_the_memo(db, checked):
    company_id, out = checked
    fp = by_quote(out["claims"], "Current gross margin: above 90%")["fingerprint"]
    record_review(db, company_id, fp, "confirmed", None)
    text = memo_to_markdown(build_memo(db, company_id))
    findings = text.split("## Verified findings")[1].split("## Awaiting review")[0]
    assert "Gross margin" in findings and "Investor note" not in findings and "None" not in findings


def test_reviews_made_before_reasons_were_optional_are_kept(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    old = SCHEMA.replace("""    reason       TEXT,
    item_json    TEXT,                       -- what the investor saw when deciding
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (decision = 'confirmed' OR length(trim(coalesce(reason, ''))) > 0)
);""", """    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    item_json    TEXT,                       -- what the investor saw when deciding
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);""")
    assert old != SCHEMA
    conn.executescript(old)
    cid = conn.execute("INSERT INTO companies(name) VALUES ('Acme')").lastrowid
    conn.execute("INSERT INTO reviews(company_id, item_type, fingerprint, decision, reason) VALUES (?, 'claim', 'fp',"
                 " 'dismissed', 'old reason')", (cid,))
    conn.commit()
    init_db(conn)
    assert conn.execute("SELECT reason FROM reviews").fetchone()["reason"] == "old reason"
    conn.execute("INSERT INTO reviews(company_id, item_type, fingerprint, decision) VALUES (?, 'claim', 'fp2', 'confirmed')",
                 (cid,))
