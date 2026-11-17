"""The sidebar's company list (in place of the company dropdown) and the run line above Deck check and Portfolio.
Front end only: static/app.js in Node (tests/render_ui.js) and the page files."""

import re

import pytest

from app.config import PROJECT_ROOT
from tests.test_ui_dismissed import NODE, render

pytestmark = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")

COMPANIES = [{"id": 3, "name": "ClearClaim AI", "documents": 7}, {"id": 2, "name": "Kitebridge Finance", "documents": 3},
             {"id": 1, "name": "Tidewell Insurance", "documents": 1}]


def company_list(code: str = "") -> str:
    return render(f'state.companies = DATA; {code}; renderCompanyList(); $("company-list").innerHTML', COMPANIES)


def rows(html: str) -> list[tuple[bool, str]]:
    """(highlighted, text) for each button of the list."""
    out = []
    for part in html.split("<button")[1:]:
        head, body = part.split(">", 1)
        out.append(('class="company-item active"' in head,
                    " ".join(re.sub(r"<[^>]+>", "", body.split("</button>")[0]).split())))
    return out


def test_the_page_lists_companies_instead_of_a_dropdown():
    page = (PROJECT_ROOT / "static" / "index.html").read_text()
    assert 'id="company-select"' not in page
    assert ">Companies<" in page and 'id="company-list"' in page


def test_all_companies_first_then_each_company_with_its_files():
    assert rows(company_list()) == [(True, "All companies"), (False, "ClearClaim AI · 7 files"),  # All = Auto-detect
                                    (False, "Kitebridge Finance · 3 files"), (False, "Tidewell Insurance · 1 file")]


def test_the_current_company_is_highlighted():
    html = company_list('state.current = {id: 2, name: "Kitebridge Finance"}')
    assert [text for active, text in rows(html) if active] == ["Kitebridge Finance · 3 files"]
    assert html.count('aria-pressed="true"') == 1


def test_picking_a_company_works_like_the_dropdown_and_moves_deck_check_and_portfolio():
    out = render("""state.companies = DATA; claims.companyId = 1; mon.companyId = 1;
        pickCompany("3");
        const picked = {current: state.current, claims: claims.companyId, mon: mon.companyId, html: $("company-list").innerHTML};
        pickCompany("");
        ({picked, all: {current: state.current, claims: claims.companyId, mon: mon.companyId}})""", COMPANIES)
    assert out["picked"]["current"] == {"id": 3, "name": "ClearClaim AI"}
    assert out["picked"]["claims"] == 3 and out["picked"]["mon"] == 3
    assert [text for active, text in rows(out["picked"]["html"]) if active] == ["ClearClaim AI · 7 files"]
    # All companies = Auto-detect: questions choose the company again; the open views keep theirs
    assert out["all"] == {"current": None, "claims": 3, "mon": 3}


def test_a_question_about_another_company_moves_the_highlight():
    html = render("""state.companies = DATA; renderCompanyList(); setCurrent({id: 1, name: "Tidewell Insurance"});
        $("company-list").innerHTML""", COMPANIES)
    assert [text for active, text in rows(html) if active] == ["Tidewell Insurance · 1 file"]


def test_run_line_gives_date_and_time_only():
    run = {"finished_at": "2026-11-15 20:00:00", "usage": {"estimated_usd": 0.62, "seconds": 75, "model_calls": 9,
                                                           "tokens": None, "web_searches": 4}}
    html = render("runLineHtml(DATA, 'Read the deck.')", run)
    line = html.split(">", 1)[1].split("<")[0]
    assert line == "Checked Nov 15, 2026 · 1 min"
    assert "$" not in html and "cost" not in html.lower()
