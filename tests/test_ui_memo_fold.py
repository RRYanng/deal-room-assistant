"""The memo page shows each confirmed item as one line; ▸ opens its quotes, evidence, note and Remove.
Questions for the founder show the question alone. Print, Copy and Download keep the full memo.
Front end only: static/app.js in Node (tests/render_ui.js) with the server's memo data."""

import re

import pytest

from app.claims import create_run, run_claim_check
from app.config import PROJECT_ROOT
from app.main import list_claims, memo
from app.monitor import create_run as create_monitor_run, run_monitor
from app.monitor import latest_results as monitor_results
from app.reviews import record_review
from tests.helpers_v2 import DECK, fake_web, load_clearclaim, monitor_llm, research_llm
from tests.test_ui_dismissed import NODE, render

pytestmark = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")

NOTE = "Ask for the payer mix."


@pytest.fixture
def built(db):
    """ClearClaim with every claim, change and portfolio signal confirmed; one note on the first item."""
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    run_monitor(db, create_monitor_run(db, company_id), monitor_llm())
    page = list_claims(company_id, conn=db)
    for c in page["claims"]:
        record_review(db, company_id, c["fingerprint"], "confirmed", "")
    for c in page.get("changes", []):
        record_review(db, company_id, c["fingerprint"], "confirmed", "", "drift")
    for s in monitor_results(db, company_id)["signals"]:
        record_review(db, company_id, s["fingerprint"], "confirmed", "", "signal")
    first = memo(company_id, conn=db)["items"][0]
    for fp in first["fingerprints"]:
        record_review(db, company_id, fp, "confirmed", NOTE, first["item_type"])
    return company_id, memo(company_id, conn=db)


def memo_page(company_id, data, code=""):
    return render(f"""claims.view = "memo"; claims.companyId = {company_id};
        claims.memo = {{...DATA, text: memoText(DATA.markdown)}}; {code}; renderMemo(); $("claims-body").innerHTML""", data)


def screen(html: str) -> str:
    """The page as seen on screen: without the copies only printing shows."""
    html = re.sub(r'<div class="memo-print">.*?</div>', "", html, flags=re.S)
    return re.sub(r'<span class="memo-print">.*?</span>', "", html, flags=re.S)


def text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split()).replace(" :", ":").replace(" )", ")")


def lines(html: str) -> list[str]:
    return [text(x) for x in re.findall(r'<span class="memo-line">(.*?)</span></button>', html, flags=re.S)]


def section(html: str, title: str) -> str:
    return html.split(f"<h2>{title}")[1].split("<h2>")[0]


def test_each_confirmed_item_is_one_line_and_folded(built):
    company_id, data = built
    html = memo_page(company_id, data)
    shown = lines(html)
    assert len(shown) == len(data["items"]) and html.count('aria-expanded="false"') == len(data["items"])
    assert "Contradicted · Gross margin: 67–82% on p.5 vs above 90% on p.9" in shown
    assert any(l.startswith("Contradicted · Market size: ") and "not $12B" in l for l in shown)
    assert "Changed · Number of target customers: 250,000 → 400,000" in shown
    assert any(l.startswith(("High · ", "Warning · ")) for l in shown)  # portfolio signals
    view = screen(html)
    # folded: no quotes, sources, notes or buttons on screen
    assert "Gross margin is 67-82%" not in view and "Source" not in view and NOTE not in view
    assert "data-memo-remove" not in view and "data-note-edit" not in view


def test_opening_an_item_shows_its_quotes_evidence_note_and_remove(built):
    company_id, data = built
    first = " ".join(data["items"][0]["fingerprints"])
    html = screen(memo_page(company_id, data, f'claims.memoOpen.add("{first}")'))
    assert html.count('aria-expanded="true"') == 1 and html.count("data-memo-remove") == 1
    detail = html.split('class="memo-detail">')[1]
    assert NOTE in detail and ">Remove<" in detail
    quote = text(detail.split('class="memo-item-actions"')[0])
    assert quote.startswith("&quot;There are 400,000 independent dental practices") and "p.3, para 2)" in quote
    assert "Source" in quote  # the evidence with its source
    # every item open: each has its note button and Remove, nothing else changes
    keys = ";".join(f'claims.memoOpen.add("{" ".join(i["fingerprints"])}")' for i in data["items"])
    opened = screen(memo_page(company_id, data, keys))
    assert opened.count("data-memo-remove") == len(data["items"]) and opened.count(f"Investor note: {NOTE}") == 1


def test_a_click_on_the_line_opens_and_closes_it(built):
    company_id, data = built
    key = " ".join(data["items"][0]["fingerprints"])
    out = render(f"""claims.view = "memo"; claims.companyId = {company_id};
        claims.memo = {{...DATA, text: memoText(DATA.markdown)}};
        const toggle = () => claims.memoOpen.has("{key}") ? claims.memoOpen.delete("{key}") : claims.memoOpen.add("{key}");
        toggle(); const opened = claims.memoOpen.has("{key}"); toggle();
        ({{opened, closed: !claims.memoOpen.has("{key}")}})""", data)
    assert out == {"opened": True, "closed": True}
    assert "[data-memo-fold]" in (PROJECT_ROOT / "static" / "app.js").read_text()


def test_questions_for_the_founder_show_the_question_alone(built):
    company_id, data = built
    html = memo_page(company_id, data)
    questions = [text(q) for q in re.findall(r'<span class="memo-screen"[^>]*>(.*?)</span><span class="memo-print">',
                                             section(html, "Questions for the founder"), flags=re.S)]
    assert questions and all(q.endswith("?") for q in questions)
    assert "Market size: How was $12B calculated?" in questions
    assert not any("deck says" in q or "(p." in q for q in questions)  # the background is left out
    # questions worded alike are told apart by their topic
    changed = [q for q in questions if q.endswith("What changed: the definition, the source, or the business?")]
    assert len(changed) >= 2 and len(set(changed)) == len(changed)
    assert html.count('class="memo-question"') == len(questions)


def test_print_and_exports_keep_the_full_memo(built):
    company_id, data = built
    html = memo_page(company_id, data)
    printed = "".join(re.findall(r'<(?:div|span) class="memo-print">(.*?)</(?:div|span)>', html, flags=re.S))
    assert "Gross margin is 67-82%" in printed and "Source" in printed and f"Investor note: {NOTE}" in printed
    assert "How was $12B calculated?" in printed and "(p.3, para 3)" in printed  # the question with its background
    # Copy and Download use the memo's Markdown, unchanged
    out = render("memoText(DATA.markdown)", data)
    assert "Gross margin is 67-82%" in out and f"Investor note: {NOTE}" in out and "Source" in out
    style = (PROJECT_ROOT / "static" / "style.css").read_text()
    printing = style[style.index("@media print"):]
    assert ".memo-print { display: none; }" in style
    assert "body.memo-open .memo-screen { display: none !important; }" in printing
    assert "body.memo-open div.memo-print { display: block; }" in printing


def test_investor_notes_stay_on_top(built):
    company_id, data = built
    html = memo_page(company_id, data)
    assert html.index('id="memo-notes"') < html.index('class="memo-doc"')
