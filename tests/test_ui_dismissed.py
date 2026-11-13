"""Dismissed items stay reachable: "Show dismissed (n)" at the bottom of the Issues and All claims tabs and of
the portfolio page lists them with the reason and a Change button. Runs static/app.js in Node
(tests/render_ui.js) on the data the API returns."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.claims import create_run, run_claim_check
from app.main import list_claims, monitor_results
from app.monitor import create_run as create_monitor_run
from app.monitor import run_monitor
from app.reviews import record_review
from tests.helpers_v2 import DECK, by_quote, claim_llm, load_clearclaim, monitor_llm, review_row

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")

REASON = "Margin was restated in the meeting."


def render(code: str, data) -> str:
    out = subprocess.run([NODE, str(ROOT / "tests" / "render_ui.js"), str(ROOT / "static" / "app.js")],
                         input=json.dumps({"data": data, "code": code}), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def claims_page(tab: str, data: dict, show: bool) -> str:
    return render(f"""claims.data = DATA; claims.view = "claims"; claims.tab = "{tab}";
        claims.showDismissed = {str(show).lower()}; renderClaims(); $("claims-body").innerHTML""", data)


@pytest.fixture
def dismissed_claim(db):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), claim_llm(), None)
    rows = list_claims(company_id, conn=db)["rows"]
    margin = by_quote(rows, "Gross margin is 67-82%")  # one row with "above 90%"
    assert margin["status"] == "contradicted"  # shown on the Issues tab until dismissed
    review_row(db, company_id, rows, "Gross margin is 67-82%", "dismissed", REASON)
    return margin["fingerprint"], list_claims(company_id, conn=db)


@pytest.mark.parametrize("tab", ["issues", "all"])
def test_dismissed_claims_are_listed_at_the_bottom_of_the_tab(dismissed_claim, tab):
    fp, data = dismissed_claim
    card = f'data-card="{fp}"'

    hidden = claims_page(tab, data, show=False)
    assert card not in hidden
    assert REASON not in hidden
    toggle = hidden.find('id="claims-toggle-dismissed"')
    assert toggle > hidden.rfind("data-card="), "the toggle is below the last card"
    assert "Show dismissed (1)" in " ".join(hidden[toggle:].split())

    shown = claims_page(tab, data, show=True)
    assert "Hide dismissed (1)" in " ".join(shown.split())
    dismissed = shown[shown.find('id="claims-toggle-dismissed"'):]
    assert card in dismissed  # folded, with the reason and Change visible without opening it
    assert REASON in dismissed and f'data-change="start" data-type="claim" data-fp="{fp}"' in dismissed


def test_dismissed_signals_are_listed_at_the_bottom_of_the_portfolio_page(db):
    company_id, _ = load_clearclaim(db, stage=("diligence", "portfolio"))
    run_monitor(db, create_monitor_run(db, company_id), monitor_llm())
    q2 = next(s for s in monitor_results(company_id, conn=db)["signals"] if s["period_end"] == "2026-06-30")
    record_review(db, company_id, q2["fingerprint"], "dismissed", "Bridge round closed in July.", item_type="signal")
    data = monitor_results(company_id, conn=db)

    def page(show: bool) -> str:
        return render(f"""mon.data = DATA; mon.companyId = 1; mon.showDismissed = {str(show).lower()};
            renderMonitor(); $("monitor-body").innerHTML""", data)

    hidden = page(False)
    assert "Bridge round closed in July." not in hidden
    toggle = hidden.find('id="monitor-toggle-dismissed"')
    assert toggle > hidden.rfind('class="issue-card signal'), "the toggle is below the last signal"
    assert "Show dismissed (1)" in " ".join(hidden[toggle:].split())

    dismissed = page(True)
    dismissed = dismissed[dismissed.find('id="monitor-toggle-dismissed"'):]
    assert "Bridge round closed in July." in dismissed
    assert f'data-change="start" data-type="signal" data-fp="{q2["fingerprint"]}"' in dismissed
