"""Portfolio monitoring (E14): each risk signal appears in the quarter where it happens, and not before."""

import time

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app, get_extract_llm
from app.monitor import create_run, latest_results, run_monitor, signals_for
from tests.helpers import load_samples
from tests.helpers_v2 import MONITOR_RULES, load_clearclaim, monitor_llm

STAGES = ("diligence", "portfolio")


def monitor(db, company_id, llm=None):
    run_monitor(db, create_run(db, company_id), llm or monitor_llm())
    return latest_results(db, company_id)


@pytest.fixture
def results(db):
    company_id, _ = load_clearclaim(db, stage=STAGES)
    return monitor(db, company_id)


def by_period(results):
    out = {}
    for s in results["signals"]:
        out.setdefault(s["period_end"], []).append((s["kind"], s["level"]))
    return out


def test_e14_each_signal_appears_in_its_quarter_and_nothing_in_q1(results):
    assert [p["period_end"] for p in results["periods"]] == ["2026-03-31", "2026-06-30", "2026-09-30"]
    assert by_period(results) == {
        "2026-06-30": [("cash_runway", "warning")],
        "2026-09-30": [("cash_runway", "high"), ("key_person_departure", "high"), ("milestone_slip", "warning")],
    }


def test_e14_runway_is_calculated_and_explained(results):
    q2, q3 = (s for s in results["signals"] if s["kind"] == "cash_runway")
    assert q2["title"] == "Cash runway 11.0 months"
    assert "$3.3M cash / $300,000 monthly net burn = 11.0 months of runway (calculated)" in q2["detail"]
    assert "Revenue growth 15.2% (calculated from the two updates)" in q2["detail"]
    assert "Revenue growth 2.0%" in q3["detail"] and "under 10%" in q3["detail"]
    assert [p["runway_months"] for p in results["periods"]] == [22.0, 11.0, 7.0]


def test_e14_departure_and_slip_name_their_evidence(results):
    departure = next(s for s in results["signals"] if s["kind"] == "key_person_departure")
    assert departure["title"] == "CTO Daniel Okafor departed"
    assert any("left the company in August" in e["quote"] for e in departure["evidence"])
    slip = next(s for s in results["signals"] if s["kind"] == "milestone_slip")
    assert "2026-05-31 -> 2026-08-31 -> 2026-11-30" in slip["detail"]
    assert {e["filename"] for e in slip["evidence"]} == {f"clearclaim_update_2026-Q{q}.txt" for q in (1, 2, 3)}


def test_meeting_notes_without_cash_burn_or_revenue_are_not_a_period(results):
    assert all(p["filename"] != "clearclaim_meeting_notes.md" for p in results["periods"])
    assert any("clearclaim_meeting_notes.md reports no cash, burn or revenue" in n for n in results["run"]["notes"])


def test_an_altered_quote_is_dropped_and_its_signal_with_it(db):
    company_id, _ = load_clearclaim(db, stage=STAGES)
    rules = [r | {"quote": "As of June 30, 2026, we had $1.3M in cash.", "value_raw": "$1.3M"}
             if r.get("kpi") == "cash" and r["value_raw"] == "$3.3M" else r for r in MONITOR_RULES]
    out = monitor(db, company_id, monitor_llm(rules))
    assert "2026-06-30" not in by_period(out)  # no cash figure left for Q2, so no runway
    assert "cash" not in out["periods"][1]["metrics"]


def test_signals_are_the_same_on_a_second_run(db):
    company_id, _ = load_clearclaim(db, stage=STAGES)
    first = monitor(db, company_id)
    second = monitor(db, company_id)
    assert first["run"]["id"] != second["run"]["id"]
    assert [s["fingerprint"] for s in first["signals"]] == [s["fingerprint"] for s in second["signals"]]


def test_monitoring_reads_only_the_company_s_own_files(db):
    load_samples(db, include_update=True)
    company_id, _ = load_clearclaim(db, stage=STAGES)
    llm = monitor_llm()
    monitor(db, company_id, llm)
    prompts = " ".join(c["prompt"] for c in llm.calls)
    assert "Tidewell" not in prompts and "Kitebridge" not in prompts
    assert len(llm.calls) == 4  # meeting notes + three updates


# --- the rules on their own -------------------------------------------------------------------

def period(end, cash=None, burn=None, revenue=None, people=(), milestones=()):
    def metric(v):
        return {"value": v, "value_raw": f"{v:,}", "quote": "q", "filename": "f", "loc_label": "f", "chunk_id": 1}
    metrics = {k: metric(v) for k, v in (("cash", cash), ("monthly_net_burn", burn), ("revenue", revenue)) if v}
    return {"period_end": end, "metrics": metrics, "people": list(people), "milestones": list(milestones)}


def person(name, role, status="present"):
    return {"name": name, "role": role, "status": status, "quote": "q", "filename": "f", "loc_label": "f", "chunk_id": 1}


@pytest.mark.parametrize("cash, burn, revenues, expected", [
    (2_400_000, 200_000, (100, 105), None),       # exactly 12 months: not under 12
    (2_300_000, 200_000, (100, 115), "warning"),  # 11.5 months, growth 15%
    (2_300_000, 200_000, (100, 105), "high"),     # 11.5 months, growth 5%
    (2_300_000, 200_000, (None, None), "warning"),  # growth unknown: stays warning
    (6_000_000, 200_000, (100, 101), None),       # 30 months
])
def test_runway_levels(cash, burn, revenues, expected):
    periods = [period("2026-03-31", revenue=revenues[0]), period("2026-06-30", cash, burn, revenues[1])]
    runway = [s for s in signals_for(periods, 1) if s["kind"] == "cash_runway"]
    assert [s["level"] for s in runway] == ([expected] if expected else [])


def test_absence_counts_as_departure_only_when_the_update_lists_the_team():
    before = period("2026-03-31", people=[person("Daniel Okafor", "CTO"), person("Ana Ruiz", "Engineer")])
    silent = period("2026-06-30")  # no team list: says nothing about who left
    listed = period("2026-06-30", people=[person("Maya Chen", "CEO")])
    assert not [s for s in signals_for([before, silent], 1) if s["kind"] == "key_person_departure"]
    departed = [s["title"] for s in signals_for([before, listed], 1) if s["kind"] == "key_person_departure"]
    assert departed == ["CTO Daniel Okafor departed"]  # the engineer is not a key role


def test_overdue_milestone_is_a_slip():
    m = {"name": "Launch", "target_date": "2026-05-31", "status": "planned", "quote": "q", "filename": "f",
         "loc_label": "f", "chunk_id": 1}
    signals = signals_for([period("2026-06-30", milestones=[m])], 1)
    assert [s["detail"] for s in signals] == ["Launch: target date 2026-05-31 passed and it is not done."]


# --- API ----------------------------------------------------------------------------------------

def test_api_monitoring(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(path))
    with TestClient(app) as client:
        conn = connect(path)
        company_id, _ = load_clearclaim(conn, stage=STAGES)
        conn.close()
        app.dependency_overrides[get_extract_llm] = monitor_llm
        started = client.post(f"/api/companies/{company_id}/monitor")
        assert started.status_code == 202
        for _ in range(300):
            run = client.get(f"/api/companies/{company_id}/monitor-runs/{started.json()['id']}").json()
            if run["status"] != "running":
                break
            time.sleep(0.1)
        assert run["status"] == "done" and run["usage"]["model_calls"] == 4
        out = client.get(f"/api/companies/{company_id}/monitor").json()
        assert len(out["signals"]) == 4
        empty = client.post("/api/companies", json={"name": "No Updates Co"}).json()["id"]
        assert client.post(f"/api/companies/{empty}/monitor").status_code == 400
