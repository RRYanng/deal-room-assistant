"""The memo follows a VC investment memo and only reorganizes checked results."""

import re

import pytest

from app.claims import create_run, run_claim_check
from app.memo import build_memo, founder_questions, memo_to_markdown
from app.monitor import create_run as create_monitor_run, run_monitor
from app.reviews import record_review
from tests.helpers_v2 import DECK, PRICING, by_quote, fake_web, load_clearclaim, monitor_llm, research_llm, review_row

SECTIONS = ["## Summary", "## Market", "## Competition", "## Verified findings (confirmed by the investment team)",
            "## Risks", "## Portfolio signals", "## Awaiting review", "## Questions for the founder"]


@pytest.fixture
def memo(db):
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    run_monitor(db, create_monitor_run(db, company_id), monitor_llm())
    return company_id, build_memo(db, company_id)


def section(text, heading):
    return text.split(heading)[1].split("\n## ")[0]


def test_sections_come_in_memo_order(memo):
    text = memo_to_markdown(memo[1])
    positions = [text.index(s) for s in SECTIONS]
    assert positions == sorted(positions)


def test_summary_is_one_line_built_from_the_counts(memo):
    lines = [l for l in section(memo_to_markdown(memo[1]), "## Summary").splitlines() if l.strip()]
    assert len(lines) == 1
    assert re.match(r"\d+ claims checked: \d+ contradicted, \d+ possibly overstated, \d+ unverified, \d+ supported; "
                    r"0 confirmed by the team\. Most important: Number of target customers", lines[0])


def test_risks_hold_the_regulatory_checklist_and_alignment(memo):
    risks = section(memo_to_markdown(memo[1]), "## Risks")
    assert "**HIPAA (patient health information)**: Not addressed in materials." in risks
    assert "**Business Associate Agreement (BAA)**: Not addressed in materials." in risks
    assert "it is not a finding of non-compliance" in risks
    assert f'How it charges: "{PRICING}"' in risks
    assert "Part of the price is a share of amounts recovered" in risks and "not a problem in themselves" in risks


def test_questions_collect_every_kind_and_nothing_dismissed(db, memo):
    company_id, built = memo
    questions = founder_questions(built)
    text = " ".join(questions)
    assert "Gross margin is stated as 67-82%" in text           # claim
    assert "in the earlier deck" in text                         # change since the last deck
    assert "How are claims chosen for resubmission" in text      # alignment
    assert "Business Associate Agreement with each practice" in text  # regulatory
    assert "moved from May 31, 2026 to November 30, 2026" in text  # portfolio signal
    assert len(questions) == len(set(questions))

    review_row(db, company_id, built["claims"], "Gross margin is 67-82%", "dismissed", "Explained in the meeting.")
    again = founder_questions(build_memo(db, company_id))
    assert not any("Gross margin is stated" in q for q in again)


def test_verified_findings_are_grouped_by_category(db, memo):
    company_id, built = memo
    record_review(db, company_id, by_quote(built["claims"], "400,000 independent")["fingerprint"], "confirmed", "")
    review_row(db, company_id, built["claims"], "team of 14", "confirmed", "Asked for payroll.")
    findings = section(memo_to_markdown(build_memo(db, company_id)), "## Verified findings")
    assert findings.index("### Market") < findings.index("### Team")
    assert "Investor note: Asked for payroll." in findings and "Investor note: \n" not in findings
