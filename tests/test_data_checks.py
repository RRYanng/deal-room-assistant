"""Data checks by period (app/rules.py, app/data_checks.json), on ClearClaim's metrics as stored in the demo
database: a number's date is its own, else its file's, else unknown. Conflicts = same file, a market fact two
files disagree on, or dates within 31 days; Needs review = a date unknown; Changed over time = further apart."""

import json

import pytest

from app import rules
from app.agent import related_checks
from app.brief import brief_to_markdown
from app.main import get_checks, list_issues
from tests.helpers_v2 import load_clearclaim
from tests.test_ui_dismissed import NODE, render

# (file, metric, label, value_raw, value, unit, currency, as_of_date, quote): ClearClaim in data/demo_v2.db
CLEARCLAIM_METRICS = [
    ('clearclaim_deck_2025-09.pdf', 'paying_customers', None, '40', 40.0, 'count', None, '2025-08-31', 'As of August 31, 2025, 40 dental practices pay for ClearClaim.'),
    ('clearclaim_deck_2025-09.pdf', 'other', 'full-time employees', '9', 9.0, 'employees', None, None, 'We are a team of 9 full-time employees.'),
    ('clearclaim_deck_2025-09.pdf', 'other', 'total addressable market', '$900M', 900000000.0, 'usd', 'USD', None, 'At $3,600 per practice per year, our total addressable market is $900M.'),
    ('clearclaim_deck_2025-09.pdf', 'other', 'price per practice per year', '$3,600', 3600.0, 'usd', 'USD', None, 'At $3,600 per practice per year, our total addressable market is $900M.'),
    ('clearclaim_deck_2025-09.pdf', 'other', 'independent dental practices in US', '250,000', 250000.0, 'practices', None, None, 'There are 250,000 independent dental practices in the United States.'),
    ('clearclaim_deck_2025-09.pdf', 'other', 'US annual dental care spend', '$400 billion', 400000000000.0, 'usd', 'USD', None, 'Americans spend $400 billion a year on dental care.'),
    ('clearclaim_deck_2025-12.pdf', 'paying_customers', None, '85', 85.0, 'count', None, '2025-11-30', 'As of November 30, 2025, 85 dental practices pay for ClearClaim.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'US dental care spending per year', '$400 billion', 400000000000.0, 'usd', 'USD', None, 'Americans spend $400 billion a year on dental care.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Independent dental practices in US', '400,000', 400000.0, 'practices', None, None, 'There are 400,000 independent dental practices in the United States.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Price per practice per year', '$3,600', 3600.0, 'usd', 'USD', None, 'At $3,600 per practice per year, our total addressable market is $12B.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Total addressable market', '$12B', 12000000000.0, 'usd', 'USD', None, 'At $3,600 per practice per year, our total addressable market is $12B.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Staff hours saved per week per practice', '15', 15.0, 'hours', None, None, 'ClearClaim saves front-desk staff 15 hours per week per practice.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Gross margin', '67-82%', 67.0, '%', None, None, 'Gross margin is 67-82%, depending on payer mix and the share of claims that need manual review.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Full-time employees', '14', 14.0, 'employees', None, None, 'We are a team of 14 full-time employees.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Annual operating costs 2025', '$420,000', 420000.0, 'usd', 'USD', None, 'Total annual operating costs for 2025 are $420,000.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Current gross margin', 'above 90%', 90.0, '%', None, None, 'Current gross margin: above 90%.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Assumed customer churn (model)', '0%', 0.0, '%', None, None, 'Our model assumes 0% customer churn and 98% first-pass claim acceptance through 2029.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Assumed first-pass claim acceptance (model)', '98%', 98.0, '%', None, None, 'Our model assumes 0% customer churn and 98% first-pass claim acceptance through 2029.'),
    ('clearclaim_deck_2025-12.pdf', 'other', 'Fee on recovered denied claims', '8%', 8.0, '%', None, None, 'Practices pay a monthly subscription per location, plus 8% of the amounts ClearClaim recovers on previously denied claims.'),
    ('clearclaim_meeting_notes.md', 'paying_customers', None, '85', 85.0, 'count', None, '2025-11-30', 'Maya said that as of November 30, 2025, 85 dental practices were paying for ClearClaim.'),
    ('clearclaim_meeting_notes.md', 'other', 'gross margin', '70%', 70.0, '%', None, None, 'Maya said gross margin is around 70% today, because about a quarter of claims still need manual review by the operations team.'),
    ('clearclaim_meeting_notes.md', 'other', 'share of claims needing manual review', 'a quarter', None, 'claims', None, None, 'Maya said gross margin is around 70% today, because about a quarter of claims still need manual review by the operations team.'),
    ('clearclaim_crm.csv', 'paying_customers', None, '85', 85.0, 'count', None, '2025-11-30', 'Reviewed traction: 85 paying practices as of November 30, 2025.'),
    ('clearclaim_update_2026-Q1.txt', 'other', 'cash', '$4.4M', 4400000.0, 'usd', 'USD', '2026-03-31', 'As of March 31, 2026, we had $4.4M in cash.'),
    ('clearclaim_update_2026-Q1.txt', 'other', 'average monthly net burn', '$200,000', 200000.0, 'usd', 'USD', None, 'Net burn averaged $200,000 per month in Q1.'),
    ('clearclaim_update_2026-Q1.txt', 'revenue', None, '$310,000', 310000.0, 'USD', 'USD', None, 'Revenue for Q1 2026 was $310,000, up 15% from Q4 2025.'),
    ('clearclaim_update_2026-Q1.txt', 'other', 'revenue growth QoQ', '15%', 15.0, '%', None, None, 'Revenue for Q1 2026 was $310,000, up 15% from Q4 2025.'),
    ('clearclaim_update_2026-Q1.txt', 'paying_customers', None, '120', 120.0, 'count', None, None, 'We ended the quarter with 120 paying practices.'),
    ('clearclaim_update_2026-Q2.txt', 'other', 'cash', '$3.3M', 3300000.0, 'usd', 'USD', '2026-06-30', 'As of June 30, 2026, we had $3.3M in cash.'),
    ('clearclaim_update_2026-Q2.txt', 'other', 'monthly net burn', '$300,000', 300000.0, 'usd', 'USD', None, 'Net burn averaged $300,000 per month in Q2, as we added two engineers.'),
    ('clearclaim_update_2026-Q2.txt', 'other', 'engineers added', 'two', 2.0, 'engineers', None, None, 'Net burn averaged $300,000 per month in Q2, as we added two engineers.'),
    ('clearclaim_update_2026-Q2.txt', 'revenue', None, '$357,000', 357000.0, 'USD', 'USD', None, 'Revenue for Q2 2026 was $357,000, up 15% from Q1 2026.'),
    ('clearclaim_update_2026-Q2.txt', 'other', 'revenue growth QoQ', '15%', 15.0, '%', None, None, 'Revenue for Q2 2026 was $357,000, up 15% from Q1 2026.'),
    ('clearclaim_update_2026-Q2.txt', 'paying_customers', None, '138', 138.0, 'count', None, None, 'We ended the quarter with 138 paying practices.'),
    ('clearclaim_update_2026-Q3.txt', 'other', 'cash', '$2.1M', 2100000.0, 'usd', 'USD', '2026-09-30', 'As of September 30, 2026, we had $2.1M in cash.'),
    ('clearclaim_update_2026-Q3.txt', 'other', 'average monthly net burn', '$300,000', 300000.0, 'usd', 'USD', None, 'Net burn averaged $300,000 per month in Q3.'),
    ('clearclaim_update_2026-Q3.txt', 'revenue', None, '$364,000', 364000.0, 'USD', 'USD', None, 'Revenue for Q3 2026 was $364,000, up 2% from Q2 2026.'),
    ('clearclaim_update_2026-Q3.txt', 'other', 'revenue growth QoQ', '2%', 2.0, '%', None, None, 'Revenue for Q3 2026 was $364,000, up 2% from Q2 2026.'),
    ('clearclaim_update_2026-Q3.txt', 'paying_customers', None, '141', 141.0, 'count', None, None, 'We ended the quarter with 141 paying practices.'),
]


def store(conn, company_id, docs, rows):
    for filename, metric, label, raw, value, unit, currency, as_of, quote in rows:
        chunk = conn.execute("SELECT id FROM chunks WHERE document_id = ? AND instr(text, ?) ORDER BY chunk_index",
                             (docs[filename], quote)).fetchone()
        assert chunk, quote
        conn.execute("INSERT INTO metrics(company_id, document_id, chunk_id, metric, metric_label, value_raw, value,"
                     " unit, currency, as_of_date, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (company_id, docs[filename], chunk["id"], metric, label, raw, value, unit, currency, as_of, quote))
    conn.commit()


@pytest.fixture
def clearclaim(db):
    """ClearClaim with all seven files and the demo database's metrics. Returns (company_id, docs)."""
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    store(db, company_id, docs, CLEARCLAIM_METRICS)
    return company_id, docs


def named(rows, name):
    found = [r for r in rows if (r["metric_label"] or r["metric"]).lower() == name.lower()]
    assert len(found) <= 1
    return found[0] if found else None


def test_clearclaim_counts(db, clearclaim):
    company_id, _ = clearclaim
    checks = get_checks(company_id, conn=db)
    assert {g: len(v) for g, v in checks.items()} == {"conflicts": 3, "needs_review": 0, "changed": 6, "missing": 2}
    issues = next(c for c in list_issues(conn=db) if c["id"] == company_id)
    assert issues["total"] == 5  # conflicts + needs review + missing; changes over time are not issues
    assert "unclear_basis" not in checks and "unclear_basis" not in issues


def test_the_three_conflicts_with_each_value_its_file_and_page(db, clearclaim):
    conflicts = get_checks(clearclaim[0], conn=db)["conflicts"]
    summaries = {(c["metric_label"] or c["metric"]): (c["summary"], c["reason"]) for c in conflicts}
    assert summaries == {
        "Independent dental practices in US": ("250,000 (Sep 2025 deck, p.3) vs 400,000 (Dec 2025 deck, p.3)",
                                               "Market fact: the files disagree"),
        "Total addressable market": ("$900M (Sep 2025 deck, p.3) vs $12B (Dec 2025 deck, p.3)",
                                     "Market fact: the files disagree"),
        "Gross margin": ("67-82% (Dec 2025 deck, p.5) vs above 90% (Dec 2025 deck, p.9) vs 70% (Dec 2025 notes, para 5)",
                         "Different values in the same file; dates within 31 days: the same period"),
    }
    margin = named(conflicts, "Gross margin")
    assert [s["filename"] for s in margin["sources"]] == ["clearclaim_deck_2025-12.pdf"] * 2 + ["clearclaim_meeting_notes.md"]
    assert all(s["quote"] and s["loc_label"] for c in conflicts for s in c["sources"])


def test_growth_is_a_change_over_time_not_a_conflict(db, clearclaim):
    checks = get_checks(clearclaim[0], conn=db)
    conflicted = {(c["metric_label"] or c["metric"]).lower() for c in checks["conflicts"]}
    assert not conflicted & {"paying_customers", "cash", "revenue", "monthly net burn", "average monthly net burn"}
    timelines = {(c["metric_label"] or c["metric"]): c["summary"] for c in checks["changed"]}
    assert timelines["paying_customers"] == ("40 (Aug 2025) → 85 (Nov 2025) → 120 (Apr 2026, file date) → "
                                             "138 (Jul 2026, file date) → 141 (Oct 2026, file date)")
    assert timelines["Cash"] == "$4.4M (Mar 2026) → $3.3M (Jun 2026) → $2.1M (Sep 2026)"
    assert timelines["revenue"].startswith("$310,000 (Apr 2026, file date) → $357,000")
    assert "Full-time employees" in timelines and "Gross margin" not in timelines  # all its dates are one period
    paying = named(checks["changed"], "paying_customers")
    assert len(paying["sources"]) == 7  # 85 is stated in three files; every source is kept


def test_dates_within_31_days_are_one_period(db, clearclaim):
    company_id, docs = clearclaim
    quote = "We are a team of 14"
    store(db, company_id, docs, [
        ("clearclaim_deck_2025-12.pdf", "other", "churn", "2%", 2.0, "%", None, "2026-01-01", quote),
        ("clearclaim_meeting_notes.md", "other", "churn", "3%", 3.0, "%", None, "2026-02-01", "gross margin"),
        ("clearclaim_deck_2025-12.pdf", "other", "nps", "40", 40.0, "points", None, "2026-01-01", quote),
        ("clearclaim_meeting_notes.md", "other", "nps", "45", 45.0, "points", None, "2026-02-02", "gross margin"),
    ])
    checks = get_checks(company_id, conn=db)
    churn = named(checks["conflicts"], "churn")
    assert churn and churn["reason"] == "Dates within 31 days: the same period"  # 31 days apart
    assert named(checks["changed"], "nps") and not named(checks["conflicts"], "nps")  # 32 days apart


def test_a_number_with_no_date_at_all_needs_review(db, clearclaim):
    company_id, docs = clearclaim
    db.execute("UPDATE documents SET doc_date = NULL WHERE id = ?", (docs["clearclaim_update_2026-Q3.txt"],))
    checks = get_checks(company_id, conn=db)
    revenue = named(checks["needs_review"], "revenue")
    assert revenue["note"] == "Dates unknown, can't tell if this is growth or a conflict."
    assert "$364,000 (date unknown; clearclaim_update_2026-Q3.txt, para 3)" in revenue["summary"]
    assert not named(checks["conflicts"], "revenue")
    assert named(checks["changed"], "revenue")["summary"].count("→") == 1  # the two dated values still show the change


def test_market_facts_come_from_the_config_file(db, clearclaim, tmp_path, monkeypatch):
    config = json.loads(rules.CONFIG_PATH.read_text())
    config["market_facts"].remove("independent dental practices in us")
    path = tmp_path / "data_checks.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(rules, "CONFIG_PATH", path)
    checks = get_checks(clearclaim[0], conn=db)
    assert not named(checks["conflicts"], "independent dental practices in US")
    assert named(checks["changed"], "independent dental practices in US")  # Sep 15 and Dec 1: 77 days apart


def test_answers_only_get_the_checks_their_sources_touch(db, clearclaim):
    company_id, _ = clearclaim
    margin = named(get_checks(company_id, conn=db)["conflicts"], "Gross margin")
    related = related_checks(db, company_id, {margin["sources"][0]["chunk_id"]})
    assert set(related) == {"conflicts", "needs_review"}
    assert [c["metric_label"] for c in related["conflicts"]] == ["Gross margin"]
    assert related_checks(db, company_id, set()) == {"conflicts": [], "needs_review": []}


def test_brief_markdown_lists_conflicts_review_and_changes(db, clearclaim):
    company_id, docs = clearclaim
    db.execute("UPDATE documents SET doc_date = NULL WHERE id = ?", (docs["clearclaim_update_2026-Q3.txt"],))
    checks = get_checks(company_id, conn=db)
    md = brief_to_markdown({"company": "ClearClaim AI", "requirements": "all", "generated_at": "now", "sections": [],
                            "key_metrics": [], "recent_communications": [], "missing": [], "open_items": [],
                            "conflicts": checks["conflicts"], "needs_review": checks["needs_review"],
                            "changed": checks["changed"]})
    assert ("- Independent dental practices in US: 250,000 (Sep 2025 deck, p.3) vs 400,000 (Dec 2025 deck, p.3). "
            "Market fact: the files disagree; not resolved:") in md
    assert "## Needs review" in md and "Dates unknown, can't tell if this is growth or a conflict." in md
    assert "## Changed over time" in md and "- Cash: $4.4M (Mar 2026) → $3.3M (Jun 2026) → $2.1M (Sep 2026)" in md
    assert "Numbers without a date" not in md and "¶" not in md


# --- page (static/app.js in Node) ------------------------------------------------------------

needs_node = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")


@needs_node
def test_the_data_checks_drawer(db, clearclaim):
    company_id, _ = clearclaim
    checks = get_checks(company_id, conn=db)
    issues = [c for c in list_issues(conn=db) if c["id"] == company_id]
    out = render(f"""(async () => {{
        fetch = async (url) => ({{ ok: true, json: async () => url.includes("/checks") ? DATA.checks : DATA.issues }});
        return {{ list: await issueListHtml(), detail: await issueDetailHtml({company_id}) }};
    }})()""", {"checks": checks, "issues": issues})
    assert "ClearClaim AI · 5 issues" in out["list"] and "3 conflicts, 0 to review, 2 missing" in out["list"]
    detail = out["detail"]
    order = [detail.index(t) for t in ("Conflicts (3)", "Needs review (0)", "Changed over time (6)", "Missing (2)")]
    assert order == sorted(order)
    assert "250,000 (Sep 2025 deck, p.3) vs 400,000 (Dec 2025 deck, p.3)" in detail
    assert '<details class="drawer-section drawer-fold">' in detail  # Changed over time starts folded
    assert "40 (Aug 2025) → 85 (Nov 2025)" in detail
    assert "without a date" not in detail and "¶" not in detail


@needs_node
def test_answer_boxes_show_conflicts_and_a_folded_needs_review(db, clearclaim):
    company_id, docs = clearclaim
    db.execute("UPDATE documents SET doc_date = NULL WHERE id = ?", (docs["clearclaim_update_2026-Q3.txt"],))
    checks = get_checks(company_id, conn=db)
    answer = {"claims": [], "evidence_groups": [], "open_items": [], "tool_calls": [], "run_id": "r1",
              "conflicts": [named(checks["conflicts"], "Gross margin")], "needs_review": checks["needs_review"]}
    html = render("answerHtml(DATA, 4)", answer)
    assert "Conflicting information" in html
    assert "67-82% (Dec 2025 deck, p.5) vs above 90% (Dec 2025 deck, p.9) vs 70% (Dec 2025 notes, para 5)" in html
    review = html[html.index(">Needs review ("):]
    assert review.startswith(f">Needs review ({len(checks['needs_review'])})") and '<ul id="review-4" hidden>' in html
    assert "Numbers without a date" not in html


def test_brief_to_dos_name_each_conflict_mid_sentence(db, clearclaim):
    from app.brief import clean_open_items
    conflicts = get_checks(clearclaim[0], conn=db)["conflicts"]
    assert clean_open_items([], [], conflicts) == [
        "Confirm which total addressable market figure is correct ($900M vs $12B)",
        "Confirm which independent dental practices in US figure is correct (250,000 practices vs 400,000 practices)",
        "Confirm which gross margin figure is correct (67-82% vs 70% vs above 90%)"]
