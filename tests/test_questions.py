"""Questions for the founder: one per flagged item, built only from verified quotes and figures."""

import pytest

from app.claims import create_run, latest_results, run_claim_check
from app.monitor import create_run as create_monitor_run, latest_results as monitor_results, run_monitor
from app.questions import numbers_in, only_verified_numbers
from tests.helpers_v2 import DECK, by_quote, fake_web, load_clearclaim, monitor_llm, research_llm


@pytest.fixture
def checked(db):
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    run_monitor(db, create_monitor_run(db, company_id), monitor_llm())
    return latest_results(db, company_id), monitor_results(db, company_id)


def all_questions(checked):
    claims, monitor = checked
    items = claims["claims"] + claims["changes"] + monitor["signals"]
    return [i["question"] for i in items if i.get("question")]


def test_every_flagged_item_has_a_question(checked):
    claims, monitor = checked
    flagged = [c for c in claims["claims"] if c["status"] in ("contradicted", "possibly_overstated")]
    assert flagged and all(c["question"] for c in flagged)
    assert all(c["question"] for c in claims["changes"] if c["kind"] == "changed")
    assert all(s["question"] for s in monitor["signals"])
    assert all(c["question"] is None for c in claims["claims"] if c["status"] not in ("contradicted", "possibly_overstated"))


def test_no_question_contains_a_number_its_quotes_do_not(checked):
    questions = all_questions(checked)
    assert len(questions) >= 15
    for q in questions:
        assert only_verified_numbers(q["text"], q["sources"]), q["text"]
        assert q["sources"], q["text"]


def test_questions_read_as_questions_and_quote_the_figures(checked):
    claims, monitor = checked
    margin = by_quote(claims["claims"], "Gross margin is 67-82%")["question"]["text"]
    assert margin.startswith("Gross margin is stated as 67-82% (p.5 ¶2) and above 90% (p.9 ¶3).")
    assert margin.endswith("Which figure is current, and how is each defined?")
    practices = by_quote(claims["claims"], "400,000 independent")["question"]["text"]
    assert practices.startswith("ADA reports 205,088, which sets an upper limit for this figure, but the deck says 400,000")
    size = next(c for c in claims["claims"] if c["key"] == "market.size_usd")["question"]["text"]
    assert "400,000 target customers at $3,600 each and a market size of $12B" in size
    assert "1,440,000,000" not in size  # the program's own calculation is not quoted to the founder
    change = next(c for c in claims["changes"] if c["key"] == "market.customer_count")["question"]["text"]
    assert change.startswith("Number of target customers was 250,000 in the earlier deck")
    slip = next(s for s in monitor["signals"] if s["kind"] == "milestone_slip")["question"]["text"]
    assert "moved from May 31, 2026 to November 30, 2026" in slip
    runway = next(s for s in monitor["signals"] if s["kind"] == "cash_runway")["question"]["text"]
    assert "11.0" not in runway and "months" not in runway  # the runway is calculated, so it is not put to the founder
    assert all(q["text"].rstrip().endswith("?") for q in all_questions(checked))


@pytest.mark.parametrize("text, sources, ok", [
    ("The deck says 400,000 (p.3 ¶2). How was it counted?", ["There are 400,000 practices."], True),
    ("The deck says 400,000; is it closer to 300,000?", ["There are 400,000 practices."], False),
    ("Margin is 67-82% (p.5) and above 90% (p.9).", ["Gross margin is 67-82%.", "above 90%."], True),
    ("Cash was $3.3M.", ["we had $3.3M in cash."], True),
    ("Cash was $3.4M.", ["we had $3.3M in cash."], False),
])
def test_only_verified_numbers(text, sources, ok):
    assert only_verified_numbers(text, sources) is ok


def test_numbers_in_ignores_thousand_separators():
    assert numbers_in("400,000 and $3,600.") == {"400000", "3600"}
