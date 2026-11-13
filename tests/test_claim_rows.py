"""One row per finding: the two sides of a contradiction, and claims one calculation flags, are shown, counted,
reviewed and written in the memo as one item. Display only: every claim keeps its status and fingerprint."""

import pytest

from app.claims import create_run, run_claim_check
from app.main import list_claims
from app.memo import build_memo, memo_to_markdown
from app.reviews import record_review
from tests.helpers_v2 import CLAIM_RULES, DECK, claim_llm, load_clearclaim
from tests.test_ui_dismissed import NODE, render


@pytest.fixture
def checked(db):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), claim_llm(), None)
    return company_id


def row_of(page: dict, label: str) -> dict:
    found = [r for r in page["rows"] if r["label"] == label]
    assert len(found) == 1, f"one row for {label}"
    return found[0]


def test_contradiction_and_shared_calculation_are_one_row_each(db, checked):
    page = list_claims(checked, conn=db)
    assert len(page["claims"]) == len(CLAIM_RULES)  # the claims themselves are unchanged
    margin = row_of(page, "Gross margin")
    assert [m["value_raw"] for m in margin["members"]] == ["67-82%", "above 90%"]
    assert margin["status"] == "contradicted" and {m["status"] for m in margin["members"]} == {"contradicted"}
    assert margin["headline"].startswith("67-82% on p.") and " vs above 90% on p." in margin["headline"]
    team = row_of(page, "Team size / Operating costs")
    assert [m["key"] for m in team["members"]] == ["team.headcount", "finance.opex_annual_usd"]
    assert team["headline"].startswith("$420,000 / 14 = $30,000 per full-time employee")
    assert len(page["rows"]) == len(CLAIM_RULES) - 2


def test_counts_and_summary_count_rows(db, checked):
    page = list_claims(checked, conn=db)
    main = [r for r in page["rows"] if r["key"] != "other"]
    assert page["counts"]["claims"] == len(main)
    issues = {}
    for r in main:
        if r["status"] in ("contradicted", "possibly_overstated"):
            issues[r["category"]] = issues.get(r["category"], 0) + 1
    by_category = {c["category"]: c["issues"] for c in page["summary"]["categories"]}
    assert by_category["Financials"] == issues["Financials"] and by_category["Team"] == issues["Team"]
    texts = [t["text"] for t in page["summary"]["top"]]
    assert not any(t.startswith("Gross margin: Deck says") for t in texts)


def test_a_decision_on_the_row_applies_to_all_its_claims(db, checked):
    margin = row_of(list_claims(checked, conn=db), "Gross margin")
    first, second = (m["fingerprint"] for m in margin["members"])
    record_review(db, checked, first, "dismissed", "Restated in the meeting.")
    page = list_claims(checked, conn=db)
    assert row_of(page, "Gross margin")["review_state"] == "awaiting_review"  # half a row is not a decision
    record_review(db, checked, second, "dismissed", "Restated in the meeting.")
    page = list_claims(checked, conn=db)
    row = row_of(page, "Gross margin")
    assert row["review_state"] == "dismissed" and row["review"]["reason"] == "Restated in the meeting."
    assert page["counts"]["dismissed"] == 1


def test_memo_lists_the_row_once_with_both_quotes(db, checked):
    memo = build_memo(db, checked)
    flagged = [r for r in memo["awaiting_review"] if r["key"] != "other"
               and r["status"] in ("contradicted", "possibly_overstated", "unverified")]
    waiting = len(flagged) + len(memo["changes"])  # changes since the earlier deck are counted too
    assert memo["counts"]["awaiting_review"] == waiting
    text = memo_to_markdown(memo)
    assert f"## Awaiting review ({waiting})" in text
    waiting = text.split("## Awaiting review")[1].split("## Questions for the founder")[0]
    lines = waiting.splitlines()
    assert sum(line.startswith("- **") and "· Gross margin" in line for line in lines) == 1
    assert sum(line.startswith("- **") and ("· Team size" in line or "· Operating costs" in line) for line in lines) == 1
    assert '"Current gross margin: above 90%."' in waiting and '"Gross margin is 67-82%' in waiting
    team = waiting[waiting.index("· Team size / Operating costs"):]
    # both quotes, then the shared calculation once
    assert team.index('"We are a team') < team.index('"Total annual operating costs') < team.index("**$420,000 / 14")
    assert waiting.count("**$420,000 / 14 = $30,000") == 1


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_page_shows_one_card_per_row_and_reviews_every_claim(db, checked):
    page = list_claims(checked, conn=db)
    margin = row_of(page, "Gross margin")
    html = render('claims.data = DATA; claims.view = "claims"; claims.tab = "all"; renderClaims(); $("claims-body").innerHTML',
                  page)
    assert html.count('class="card-label">Gross margin<') == 1
    assert html.count('class="card-label">Team size / Operating costs<') == 1
    assert "67–82% on p." in html and " vs above 90% on p." in html
    opened = render(f'''claims.data = DATA; claims.tab = "all"; claims.openCards.add("{margin['fingerprint']}");
        renderClaims(); $("claims-body").innerHTML''', page)
    assert "Current gross margin: above 90%." in opened and "Gross margin is 67-82%" in opened
    reviewing = render(f'claims.data = DATA; openReview("{margin["fingerprint"]}", "dismissed"); claims.reviewing.fingerprints',
                       page)
    assert reviewing == [m["fingerprint"] for m in margin["members"]]
