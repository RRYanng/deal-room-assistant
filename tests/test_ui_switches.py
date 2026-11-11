"""Parts of the page switched off in config/ui.toml: still computed, left out only when the page asks."""

import pytest

from app.claims import create_run, run_claim_check
from app.main import list_claims, memo, ui_config
from tests.helpers_v2 import DECK, fake_web, load_clearclaim, research_llm

HIDE = "changes,regulatory"


@pytest.fixture
def company(db):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    return company_id


def test_the_config_file_hides_changes_and_the_regulatory_checklist():
    assert sorted(ui_config()["hidden"]) == ["changes", "regulatory"]


def test_without_hide_the_claims_api_is_unchanged(db, company):
    page = list_claims(company, conn=db)
    assert page["changes"] and page["regulatory"]["items"]
    assert "Regulatory" in [c["category"] for c in page["summary"]["categories"]]


def test_hidden_parts_leave_the_page_data(db, company):
    full, page = list_claims(company, conn=db), list_claims(company, hide=HIDE, conn=db)
    assert page["changes"] == [] and page["regulatory"] is None
    assert [c["category"] for c in page["summary"]["categories"]] == ["Market", "Financials", "Competition", "Team"]
    assert all(t["status"] not in ("changed", "not_addressed") for t in page["summary"]["top"])
    # Awaiting review no longer counts the changes; the claims themselves are untouched.
    assert page["counts"]["awaiting_review"] == full["counts"]["awaiting_review"] - len(full["changes"])
    assert [c["fingerprint"] for c in page["claims"]] == [c["fingerprint"] for c in full["claims"]]
    # The run's note about reading the earlier deck belongs to the hidden memory.
    assert any("earlier deck" in n for n in full["run"]["notes"])
    assert not any("earlier deck" in n for n in page["run"]["notes"])


def test_hidden_parts_leave_the_memo(db, company):
    full, hidden = memo(company, conn=db)["markdown"], memo(company, hide=HIDE, conn=db)
    assert "Changed since the last deck" in full and "### Regulatory" in full
    text = hidden["markdown"]
    for gone in ("Changed since the last deck", "earlier deck", "### Regulatory", "Business Associate Agreement",
                 "HIPAA"):
        assert gone not in text, gone
    assert "### Alignment" in text and "## Questions for the founder" in text
    assert hidden["counts"] == list_claims(company, hide=HIDE, conn=db)["counts"]


def test_unknown_names_in_hide_are_ignored(db, company):
    assert list_claims(company, hide="alignment,nonsense", conn=db)["changes"]
