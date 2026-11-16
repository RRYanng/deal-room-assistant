"""Under an answer, "Numbers without a date" and "Open items" start folded to one clickable line with their
count, "Numbers without a date (2) ▸"; a click opens the list. Front end only: static/app.js in Node."""

import re

import pytest

from app.config import PROJECT_ROOT
from tests.test_ui_dismissed import NODE, render

pytestmark = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")

ANSWER = {"claims": [], "evidence_groups": [], "tool_calls": [], "run_id": "r1",
          "conflicts": [{"metric": "gross_margin", "metric_label": None, "as_of_date": "2025-11-30", "values": [1, 2],
                         "sources": []}],
          "open_items": ["No churn figure in the materials.", "Pricing for 2026 not stated.", "CAC not found."],
          "unclear_basis": [{"metric": "other", "metric_label": "cash", "display_value": "$4.4M", "reason": "no date stated"},
                            {"metric": "other", "metric_label": "ARR", "display_value": "$1.2M", "reason": "no date stated"}]}


def answer(code: str = "") -> str:
    return render(f"{code}; answerHtml(DATA, 7)", ANSWER)


def box(html: str, title: str) -> str:
    return html[html.index(f">{title} (") + 1:].split("</div>")[0]


def test_both_boxes_start_folded_to_one_line_with_their_count():
    html = answer()
    for title, count, list_id in [("Numbers without a date", 2, "undated-7"), ("Open items", 3, "open-7")]:
        part = box(html, title)
        line = " ".join(re.sub(r"<[^>]+>", " ", part.split("</button>")[0]).split())
        assert line == f"{title} ({count}) ▸"
        assert f'<ul id="{list_id}" hidden>' in part and 'aria-expanded="false"' in html
    # the lists are there, only hidden; Conflicting information is not folded
    assert "no date stated" in html and "CAC not found." in html
    assert '<div class="box-title">Conflicting information</div>' in html


def test_an_opened_box_shows_its_list():
    html = answer('state.openBoxes.add("open-7")')
    assert " ".join(re.sub(r"<[^>]+>", " ", box(html, "Open items").split("</button>")[0]).split()) == "Open items (3) ▾"
    assert '<ul id="open-7" >' in html and '<ul id="undated-7" hidden>' in html


def test_a_click_opens_and_closes_the_box():
    out = render("""
        const list = { hidden: true }, arrow = { textContent: "▸" }, attrs = {}, classes = new Set();
        const button = { dataset: { box: "undated-7" }, querySelector: () => arrow,
          setAttribute: (k, v) => { attrs[k] = v; },
          parentElement: { classList: { toggle: (c, on) => on ? classes.add(c) : classes.delete(c) } } };
        document.getElementById = (id) => id === "undated-7" ? list : null;
        const click = () => toggleBox(button);
        click();
        const opened = { hidden: list.hidden, arrow: arrow.textContent, expanded: attrs["aria-expanded"],
          kept: state.openBoxes.has("undated-7"), open: classes.has("open") };
        click();
        ({ opened, closed: { hidden: list.hidden, arrow: arrow.textContent, kept: state.openBoxes.has("undated-7") } })""",
                 None)
    assert out["opened"] == {"hidden": False, "arrow": "▾", "expanded": "true", "kept": True, "open": True}
    assert out["closed"] == {"hidden": True, "arrow": "▸", "kept": False}
    app_js = (PROJECT_ROOT / "static" / "app.js").read_text()
    assert "} else if (button.dataset.box) {\n    toggleBox(button);" in app_js  # the chat's click listener
