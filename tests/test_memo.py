"""The memo holds only what the investment team confirmed: a one-line summary, verified findings by category,
confirmed portfolio signals and the questions those items raise, then how many items still await review."""

import re

import pytest

from app.claims import create_run, run_claim_check
from app.memo import build_memo, founder_questions, memo_to_markdown
from app.monitor import create_run as create_monitor_run, run_monitor
from app.reviews import record_review
from tests.helpers_v2 import DECK, by_quote, fake_web, load_clearclaim, monitor_llm, research_llm, review_row

NOTHING = "Nothing confirmed yet. Confirm findings on the Deck check page to add them here."
SECTIONS = ["## Summary", "## Verified findings (confirmed by the investment team)",
            "## Portfolio signals (confirmed by the investment team)", "## Questions for the founder"]
# Sections that listed every checked result, and the details of items nobody has reviewed
GONE = ["## Market", "## Competition", "## Risks", "## Awaiting review", "## Dismissed", "## Notes on this check",
        "### Regulatory", "### Alignment", "Molarflow", "Not addressed in materials", "Not conclusions until"]


@pytest.fixture
def memo(db):
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    run_monitor(db, create_monitor_run(db, company_id), monitor_llm())
    return company_id, build_memo(db, company_id)


def absent(text: str, gone: str) -> bool:
    """A heading is gone when no line starts with it ("## Market" is not "### Market"); other text anywhere."""
    if gone.startswith("#"):
        return not any(line.startswith(gone) for line in text.splitlines())
    return gone not in text


def section(text, heading):
    return text.split(heading)[1].split("\n## ")[0]


def awaiting_line(memo: dict) -> str:
    n = memo["counts"]["awaiting_review"]
    return f"{n} {'item' if n == 1 else 'items'} still awaiting review on the Deck check page."


def confirm_market_size_and_gross_margin(db, company_id, built):
    size = next(c for c in built["claims"] if c["key"] == "market.size_usd")
    record_review(db, company_id, size["fingerprint"], "confirmed", "")
    review_row(db, company_id, built["claims"], "Gross margin is 67-82%", "confirmed", "Founder said 70% today.")
    return size


def test_nothing_confirmed_says_so_and_gives_only_the_count(memo):
    _, built = memo
    text = memo_to_markdown(built)
    assert NOTHING in text and text.rstrip().endswith(awaiting_line(built))
    assert built["counts"]["awaiting_review"] > 1
    for gone in GONE + SECTIONS:
        assert absent(text, gone), gone
    for c in built["claims"]:  # no claim is listed, flagged or not
        for m in c.get("members", [c]):
            assert m["quote"] not in text, m["quote"]


def test_memo_holds_only_the_confirmed_findings_and_their_questions(db, memo):
    company_id, built = memo
    size = confirm_market_size_and_gross_margin(db, company_id, built)
    again = build_memo(db, company_id)
    text = memo_to_markdown(again)
    assert NOTHING not in text
    for gone in GONE:
        assert absent(text, gone), gone

    findings = section(text, "## Verified findings")
    assert findings.index("### Market") < findings.index("### Financials")
    items = [line for line in findings.splitlines() if line.startswith("- **")]
    assert len(items) == 2
    assert "· Market size:" in items[0] and "· Gross margin:" in items[1]
    assert size["quote"] in findings and "Investor note: Founder said 70% today." in findings
    assert '"Current gross margin: above 90%."' in findings  # both sides of the row
    assert "400,000 independent" not in text  # confirmed nowhere

    margin = by_quote(again["claims"], "Gross margin is 67-82%")
    expected = [size["question"]["text"], margin["members"][0]["question"]["text"]]
    assert founder_questions(again) == expected
    questions = [l for l in section(text, "## Questions for the founder").splitlines() if l.strip()]
    assert questions[:2] == [f"1. {expected[0]}", f"2. {expected[1]}"] and len(questions) == 3
    assert questions[2] == awaiting_line(again) and text.rstrip().endswith(awaiting_line(again))


def test_sections_come_in_memo_order(db, memo):
    company_id, built = memo
    confirm_market_size_and_gross_margin(db, company_id, built)
    departure = next(s for s in built["signals"] if s["kind"] == "key_person_departure")
    record_review(db, company_id, departure["fingerprint"], "confirmed", "", item_type="signal")
    text = memo_to_markdown(build_memo(db, company_id))
    positions = [text.index(s) for s in SECTIONS]
    assert positions == sorted(positions)


def test_summary_is_one_line_about_what_was_confirmed(db, memo):
    company_id, built = memo
    confirm_market_size_and_gross_margin(db, company_id, built)
    lines = [l for l in section(memo_to_markdown(build_memo(db, company_id)), "## Summary").splitlines() if l.strip()]
    assert len(lines) == 1
    assert re.fullmatch(r"2 findings confirmed by the investment team \(1 Market, 1 Financials\): "
                        r"(\d+ [a-z ]+)(, \d+ [a-z ]+)*\.", lines[0]), lines[0]


def test_only_confirmed_signals_and_their_questions(db, memo):
    company_id, built = memo
    slip = next(s for s in built["signals"] if s["kind"] == "milestone_slip")
    record_review(db, company_id, slip["fingerprint"], "confirmed", "Launch moved; asked for the new plan.",
                  item_type="signal")
    again = build_memo(db, company_id)
    text = memo_to_markdown(again)
    signals = section(text, "## Portfolio signals (confirmed by the investment team)")
    assert slip["title"] in signals and "Launch moved; asked for the new plan." in signals
    others = [s for s in built["signals"] if s["fingerprint"] != slip["fingerprint"]]
    assert others and not any(s["title"] in text for s in others)
    assert founder_questions(again) == [slip["question"]["text"]]
    assert "No deck findings confirmed yet; 1 portfolio signal confirmed." in section(text, "## Summary")


def test_dismissed_items_and_their_questions_stay_out(db, memo):
    company_id, built = memo
    review_row(db, company_id, built["claims"], "Gross margin is 67-82%", "dismissed", "Explained in the meeting.")
    text = memo_to_markdown(build_memo(db, company_id))
    assert NOTHING in text and "Explained in the meeting." not in text and "Gross margin" not in text
