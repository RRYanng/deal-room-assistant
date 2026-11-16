"""Display wording only: locations read "p.5, para 2" and "para 3" (CSV keeps "row 3") wherever they are shown,
and a metric without a date shows "no date" in the metrics table (the Data checks are in test_data_checks.py).
Stored labels and logic are
unchanged (chunks keep "p.5 ¶2"; questions and evidence are built from them as before). Also: the run dialogs
give the time only, and the Brief is stamped in local time."""

import json
import re

import pytest

from app.brief import brief_to_markdown
from app.citations import display_locations
from app.claims import create_run, run_claim_check
from app.config import PROJECT_ROOT
from app.main import get_checks, library_search, list_claims, memo
from tests.helpers_v2 import DECK, fake_web, load_clearclaim, research_llm, review_row
from tests.test_library import DECK_METRICS, store_metrics
from tests.test_ui_dismissed import NODE, render


@pytest.mark.parametrize("stored, shown", [
    ("clearclaim_deck_2025-12.pdf p.5 ¶2", "clearclaim_deck_2025-12.pdf p.5, para 2"),
    ("Gross margin is stated as 67-82% (p.5 ¶2) and above 90% (p.9 ¶3).",
     "Gross margin is stated as 67-82% (p.5, para 2) and above 90% (p.9, para 3)."),
    ("clearclaim_meeting_notes.md ¶5", "clearclaim_meeting_notes.md para 5"),
    ("Different figure in clearclaim_meeting_notes.md, ¶5: 70%.", "Different figure in clearclaim_meeting_notes.md, para 5: 70%."),
    ("deck.pdf p.2 ¶3 part 1/2", "deck.pdf p.2, para 3 part 1/2"),
    ("clearclaim_crm.csv row 3", "clearclaim_crm.csv row 3"),
])
def test_locations_are_shown_in_words(stored, shown):
    assert display_locations(stored) == shown


@pytest.fixture
def company(db):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    store_metrics(db, company_id, docs[DECK], DECK_METRICS)  # metrics without a date, as in the demo database
    claims = list_claims(company_id, conn=db)["claims"]
    review_row(db, company_id, claims, "Gross margin is 67-82%", "confirmed", "")
    return company_id


def test_memo_and_its_questions_use_words_and_the_stored_data_keeps_its_labels(db, company):
    text = memo(company, conn=db)["markdown"]
    assert "¶" not in text
    assert "(clearclaim_deck_2025-12.pdf p.5, para 2)" in text
    assert "Gross margin is stated as 67-82% (p.5, para 2) and above 90% (p.9, para 3)." in text
    assert "clearclaim_meeting_notes.md, para 5" in text
    claims = list_claims(company, conn=db)["claims"]  # logic and stored data unchanged
    assert any("p.5 ¶2" in c["loc_label"] for c in claims)
    assert any("(p.5 ¶2)" in (c.get("question") or {}).get("text", "") for c in claims)


def test_brief_markdown_uses_the_new_wording():
    brief = {"company": "Acme", "requirements": "all", "generated_at": "now",
             "sections": [{"title": "Traction", "items": [{"text": "85 paying.", "citations": [
                 {"loc_label": "deck.pdf p.3 ¶2", "quote": "85 pay."}]}]}],
             "key_metrics": [], "recent_communications": [], "missing": [], "conflicts": [], "open_items": [],
             "needs_review": [{"metric": "other", "metric_label": "cash", "summary": "$4.4M (date unknown; update.txt, para 4)",
                               "note": "Dates unknown, can't tell if this is growth or a conflict.",
                               "sources": [{"value_raw": "$4.4M", "loc_label": "update.txt ¶4", "quote": "We had $4.4M."}]}],
             "changed": []}
    md = brief_to_markdown(brief)
    assert "¶" not in md and "[deck.pdf p.3, para 2: \"85 pay.\"]" in md
    assert "## Needs review" in md and "Numbers without a date" not in md and "Unclear basis" not in md
    assert "  - $4.4M [update.txt para 4: \"We had $4.4M.\"]" in md


# --- page (static/app.js in Node) ------------------------------------------------------------

needs_node = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")


@needs_node
def test_every_html_text_shows_locations_in_words():
    assert render('esc("deck.pdf p.5 ¶2 · notes.md ¶3 · crm.csv row 3")', None) == \
        "deck.pdf p.5, para 2 · notes.md para 3 · crm.csv row 3"


@needs_node
def test_deck_check_page_and_memo_page_show_no_pilcrow(db, company):
    page = list_claims(company, conn=db)
    html = render('claims.data = DATA; claims.tab = "all"; DATA.rows.forEach((r) => claims.openCards.add(r.fingerprint));'
                  ' renderClaims(); $("claims-body").innerHTML', page)
    assert "¶" not in html and "p.5, para 2" in html and "clearclaim_meeting_notes.md · para 5" in html
    built = memo(company, conn=db)
    out = render(f'claims.view = "memo"; claims.companyId = {company}; claims.memo = {{...DATA, text: memoText(DATA.markdown)}};'
                 ' renderMemo(); ({html: $("claims-body").innerHTML, text: claims.memo.text})', built)
    assert "¶" not in out["html"] and "¶" not in out["text"]  # the page, and what Copy and Download use
    assert "(p.5, para 2) and above 90% (p.9, para 3)" in out["text"]
    assert "(clearclaim_deck_2025-12.pdf p.5, para 2)" in out["text"]  # citations are kept as written
    assert "Gross margin: 67–82% on p.5 vs above 90% on p.9" in out["text"]  # headlines name the page, as before


@needs_node
def test_answers_brief_and_library_use_the_new_wording(db, company):
    answer = render("""answerHtml({claims: [], evidence_groups: [], open_items: [], conflicts: [], tool_calls: [],
        needs_review: []}, 1)""", None)
    assert "Numbers without a date" not in answer and "Unclear basis" not in answer
    metrics = render("metricsHtml(DATA)", [{"metric": "other", "metric_label": "cash", "display_value": "$4.4M",
                                              "as_of_date": None, "quote": "q", "loc_label": "update.txt ¶4"}])
    assert ">no date<" in metrics and "unclear" not in metrics and "update.txt para 4" in metrics
    app_js = (PROJECT_ROOT / "static" / "app.js").read_text()
    assert "Unclear basis" not in app_js
    hits = render("librarySearchHtml()", None).strip()  # nothing searched yet
    assert hits == ""
    search = {"query": "gross margin", "results": library_search(q="gross margin", conn=db)["results"]}
    html = render("state.librarySearch = DATA; librarySearchHtml()", search)
    assert "p.5, para 2, p.9, para 3" in html and "¶" not in html


# --- run dialogs: time only; Brief: local time ---------------------------------------------------

@needs_node
def test_run_dialogs_give_the_time_only():
    last = {"usage": {"seconds": 190, "estimated_usd": 0.62}}
    assert render(f"runEstimate({json.dumps(last)}, 'about 3 min')", None) == \
        "It takes about 3 min, judging by the last run."
    assert render("runEstimate({usage: {seconds: 40, estimated_usd: 0.09}}, 'under 1 min')", None) == \
        "It takes under a minute, judging by the last run."
    assert render("runEstimate(null, 'about 3 min')", None) == "It takes about 3 min."
    app_js = (PROJECT_ROOT / "static" / "app.js").read_text()
    dialogs = [app_js[app_js.index(t):app_js.index("]);", app_js.index(t))] for t in ("Run deck check?", "Run monitoring?")]
    assert all(not re.search(r"\$\d|estimated_usd|cost", d) for d in dialogs)  # no amounts in either dialog
    assert "estimated_usd" not in app_js[app_js.index("function runEstimate"):app_js.index("// ---", app_js.index("function runEstimate"))]


def test_brief_is_stamped_in_local_time(monkeypatch):
    import time

    from app.brief import generate_brief
    from app.db import connect, init_db
    from tests.helpers import load_samples
    from tests.test_brief import fake_brief_llm

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        conn = connect(":memory:")
        init_db(conn)
        cid = load_samples(conn, include_update=True)["Tidewell Insurance"]
        stamp = generate_brief(conn, cid, "overview", fake_brief_llm())["generated_at"]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert stamp.endswith((" PST", " PDT")) and "UTC" not in stamp, stamp  # as the memo: "2026-11-15 15:29 PST"
