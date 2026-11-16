"""Under an answer, "Needs review" and "Open items" start folded to one clickable line with their count,
"Needs review (2) ▸"; a click opens the list. Front end only: static/app.js in Node."""

import re

import pytest

from app.config import PROJECT_ROOT
from tests.test_ui_dismissed import NODE, render

pytestmark = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")

ANSWER = {"claims": [], "evidence_groups": [], "tool_calls": [], "run_id": "r1",
          "conflicts": [{"metric": "other", "metric_label": "gross margin", "values": [1, 2], "sources": [],
                         "summary": "1% (Dec 2025 deck, p.5) vs 2% (Dec 2025 deck, p.9)",
                         "reason": "Different values in the same file"}],
          "open_items": ["No churn figure in the materials.", "Pricing for 2026 not stated.", "CAC not found."],
          "needs_review": [{"metric": "other", "metric_label": "cash", "summary": "$4.4M (date unknown; a.txt, para 2)",
                            "note": "Dates unknown, can't tell if this is growth or a conflict.", "sources": []},
                           {"metric": "other", "metric_label": "ARR", "summary": "$1.2M (date unknown; b.txt, para 1)",
                            "note": "Dates unknown, can't tell if this is growth or a conflict.", "sources": []}]}


def answer(code: str = "") -> str:
    return render(f"{code}; answerHtml(DATA, 7)", ANSWER)


def box(html: str, title: str) -> str:
    return html[html.index(f">{title} (") + 1:].split("</div>")[0]


def test_both_boxes_start_folded_to_one_line_with_their_count():
    html = answer()
    for title, count, list_id in [("Needs review", 2, "review-7"), ("Open items", 3, "open-7")]:
        part = box(html, title)
        line = " ".join(re.sub(r"<[^>]+>", " ", part.split("</button>")[0]).split())
        assert line == f"{title} ({count}) ▸"
        assert f'<ul id="{list_id}" hidden>' in part and 'aria-expanded="false"' in html
    # the lists are there, only hidden; Conflicting information is not folded
    assert "can&#39;t tell if this is growth" in html and "CAC not found." in html
    assert '<div class="box-title">Conflicting information</div>' in html


def test_an_opened_box_shows_its_list():
    html = answer('state.openBoxes.add("open-7")')
    assert " ".join(re.sub(r"<[^>]+>", " ", box(html, "Open items").split("</button>")[0]).split()) == "Open items (3) ▾"
    assert '<ul id="open-7" >' in html and '<ul id="review-7" hidden>' in html


def test_a_click_opens_and_closes_the_box():
    out = render("""
        const list = { hidden: true }, arrow = { textContent: "▸" }, attrs = {}, classes = new Set();
        const button = { dataset: { box: "review-7" }, querySelector: () => arrow,
          setAttribute: (k, v) => { attrs[k] = v; },
          parentElement: { classList: { toggle: (c, on) => on ? classes.add(c) : classes.delete(c) } } };
        document.getElementById = (id) => id === "review-7" ? list : null;
        const click = () => toggleBox(button);
        click();
        const opened = { hidden: list.hidden, arrow: arrow.textContent, expanded: attrs["aria-expanded"],
          kept: state.openBoxes.has("review-7"), open: classes.has("open") };
        click();
        ({ opened, closed: { hidden: list.hidden, arrow: arrow.textContent, kept: state.openBoxes.has("review-7") } })""",
                 None)
    assert out["opened"] == {"hidden": False, "arrow": "▾", "expanded": "true", "kept": True, "open": True}
    assert out["closed"] == {"hidden": True, "arrow": "▸", "kept": False}
    app_js = (PROJECT_ROOT / "static" / "app.js").read_text()
    assert "} else if (button.dataset.box) {\n    toggleBox(button);" in app_js  # the chat's click listener
