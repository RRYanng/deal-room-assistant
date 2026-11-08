"""Reviews and memo (E13): nothing is a conclusion until confirmed; dismissed stays quiet unless evidence changes."""

import time

import pytest
from fastapi.testclient import TestClient

from app.claims import create_run, latest_results, run_claim_check
from app.db import connect
from app.main import app, get_extract_llm, get_web
from app.memo import build_memo, memo_to_markdown
from app.reviews import ReviewError, apply_reviews, record_review, review_counts
from tests.helpers import load_samples
from tests.helpers_v2 import (CLAIM_RULES, DECK, by_quote, claim_extractor, claim_llm, fake_web, load_clearclaim,
                              research_llm)


def run(db, company_id, deck_id, llm=None, web=None):
    run_id = create_run(db, company_id, deck_id)
    run_claim_check(db, run_id, llm or claim_llm(), web)
    out = latest_results(db, company_id)
    apply_reviews(db, company_id, out["claims"])
    return out


@pytest.fixture
def checked(db):
    company_id, docs = load_clearclaim(db)
    return company_id, docs[DECK], run(db, company_id, docs[DECK])


def test_e13_flagged_items_are_not_findings_until_confirmed(db, checked):
    company_id, _, out = checked
    assert all(c["review_state"] == "awaiting_review" for c in out["claims"])
    memo = build_memo(db, company_id)
    assert memo["findings"] == []
    assert len(memo["awaiting_review"]) == len(CLAIM_RULES)
    text = memo_to_markdown(memo)
    assert "No findings confirmed yet." in text and "Not conclusions until an investor confirms them." in text


def test_e13_confirmed_item_enters_the_memo_with_the_reason(db, checked):
    company_id, _, out = checked
    margin = by_quote(out["claims"], "above 90%")
    record_review(db, company_id, margin["fingerprint"], "confirmed", "Founder confirmed 70% in the meeting.")
    memo = build_memo(db, company_id)
    assert [c["quote"] for c in memo["findings"]] == [margin["quote"]]
    assert margin["fingerprint"] not in {c["fingerprint"] for c in memo["awaiting_review"]}
    text = memo_to_markdown(memo)
    findings = text.split("## Verified findings")[1].split("## Awaiting review")[0]
    assert "Current gross margin: above 90%." in findings and "Founder confirmed 70% in the meeting." in findings


def test_e13_dismissed_item_is_not_raised_again_on_the_next_run(db, checked):
    company_id, deck_id, out = checked
    claim = by_quote(out["claims"], "15 hours per week")
    record_review(db, company_id, claim["fingerprint"], "dismissed", "Time savings are not material to the decision.")
    again = run(db, company_id, deck_id, claim_llm(claim_extractor(statement=lambda r: "New wording.")))
    same = by_quote(again["claims"], "15 hours per week")
    assert same["review_state"] == "dismissed" and not same["evidence_changed"]
    assert review_counts(again["claims"])["dismissed"] == 1
    memo = build_memo(db, company_id)
    assert same["fingerprint"] not in {c["fingerprint"] for c in memo["awaiting_review"]}
    assert "Time savings are not material" in memo_to_markdown(memo).split("## Dismissed")[1]


def test_e13_dismissed_item_is_raised_again_when_its_evidence_changes(db, checked):
    company_id, deck_id, out = checked
    claim = by_quote(out["claims"], "400,000 independent dental practices")
    assert claim["status"] == "unverified"
    record_review(db, company_id, claim["fingerprint"], "dismissed", "Market size is not the question.")
    # Web research now finds a public source: same claim, new evidence.
    again = run(db, company_id, deck_id, research_llm(), fake_web())
    same = by_quote(again["claims"], "400,000 independent dental practices")
    assert same["fingerprint"] == claim["fingerprint"]
    assert same["status"] == "contradicted"
    assert same["review_state"] == "awaiting_review" and same["evidence_changed"]
    assert same["review"]["status_then"] == "unverified"
    assert "Evidence changed since it was dismissed." in memo_to_markdown(build_memo(db, company_id))


def test_e13_a_new_number_is_a_new_item(db, checked):
    company_id, deck_id, out = checked
    claim = by_quote(out["claims"], "above 90%")
    record_review(db, company_id, claim["fingerprint"], "dismissed", "Projection, not current.")
    rules = [r | {"value_raw": "90%"} if r["value_raw"] == "above 90%" else r for r in CLAIM_RULES]
    again = run(db, company_id, deck_id, claim_llm(claim_extractor(rules)))
    changed = next(c for c in again["claims"] if c["value_raw"] == "90%")
    assert changed["fingerprint"] != claim["fingerprint"]
    assert changed["review_state"] == "awaiting_review" and changed["review"] is None


def test_e13_a_reason_is_required(db, checked):
    company_id, _, out = checked
    fp = out["claims"][0]["fingerprint"]
    for reason in ("", "   ", None):
        with pytest.raises(ReviewError, match="reason is required"):
            record_review(db, company_id, fp, "dismissed", reason)
    with pytest.raises(ReviewError):
        record_review(db, company_id, fp, "approved", "x")


def test_the_latest_decision_wins(db, checked):
    company_id, deck_id, out = checked
    fp = by_quote(out["claims"], "15 hours per week")["fingerprint"]
    record_review(db, company_id, fp, "dismissed", "Not material.")
    record_review(db, company_id, fp, "confirmed", "On reflection, ask for the time study.")
    again = run(db, company_id, deck_id)
    assert by_quote(again["claims"], "15 hours per week")["review_state"] == "confirmed"
    assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2  # history kept


def test_a_review_cannot_reach_another_company(db, checked):
    company_id, _, out = checked
    other = load_samples(db)["Tidewell Insurance"]
    with pytest.raises(ReviewError, match="No such item"):
        record_review(db, other, out["claims"][0]["fingerprint"], "dismissed", "x")


# --- API ------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(path))
    with TestClient(app) as c:
        conn = connect(path)
        c.company_id, docs = load_clearclaim(conn)
        conn.close()
        yield c


def wait_for(client, run_id, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        run = client.get(f"/api/companies/{client.company_id}/claim-runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("claim check did not finish")


def test_api_check_review_and_memo(client):
    app.dependency_overrides[get_extract_llm] = research_llm
    app.dependency_overrides[get_web] = fake_web
    cid = client.company_id
    started = client.post(f"/api/companies/{cid}/claim-check", json={})
    assert started.status_code == 202 and started.json()["filename"] == DECK
    assert wait_for(client, started.json()["id"])["status"] == "done"

    claims = client.get(f"/api/companies/{cid}/claims").json()
    statuses = {c["quote"]: c["status"] for c in claims["claims"]}
    assert statuses["Current gross margin: above 90%."] == "contradicted"
    assert statuses["There are 400,000 independent dental practices in the United States."] == "contradicted"
    assert claims["counts"]["awaiting_review"] == len(CLAIM_RULES)
    assert [c["name"] for c in claims["competitors"]] == ["Molarflow", "Toothledger"]

    fp = next(c["fingerprint"] for c in claims["claims"] if c["status"] == "contradicted")
    assert client.post(f"/api/companies/{cid}/reviews", json={"fingerprint": fp, "decision": "dismissed",
                                                             "reason": " "}).status_code == 400
    ok = client.post(f"/api/companies/{cid}/reviews", json={"fingerprint": fp, "decision": "confirmed",
                                                           "reason": "Checked with the founder."})
    assert ok.status_code == 201
    memo = client.get(f"/api/companies/{cid}/memo").json()
    assert len(memo["findings"]) == 1 and "Checked with the founder." in memo["markdown"]


def test_api_without_web_research_says_so(client):
    app.dependency_overrides[get_extract_llm] = claim_llm
    run = wait_for(client, client.post(f"/api/companies/{client.company_id}/claim-check").json()["id"])
    assert run["status"] == "done"
    assert any("Web research was not run" in n for n in run["notes"])


def test_api_company_without_a_deck(client):
    other = client.post("/api/companies", json={"name": "Empty Co"}).json()["id"]
    response = client.post(f"/api/companies/{other}/claim-check", json={})
    assert response.status_code == 400 and "no pitch deck" in response.json()["detail"]


def test_api_run_of_another_company_is_not_found(client):
    app.dependency_overrides[get_extract_llm] = claim_llm
    run_id = client.post(f"/api/companies/{client.company_id}/claim-check").json()["id"]
    wait_for(client, run_id)
    other = client.post("/api/companies", json={"name": "Other Co"}).json()["id"]
    assert client.get(f"/api/companies/{other}/claim-runs/{run_id}").status_code == 404
    assert client.get(f"/api/companies/{other}/claims").json()["claims"] == []


def test_a_run_cut_off_by_a_restart_does_not_block_new_checks(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(path))
    with TestClient(app):
        conn = connect(path)
        company_id, docs = load_clearclaim(conn)
        create_run(conn, company_id, docs[DECK])  # never started: as if the server stopped mid-run
        conn.close()
    with TestClient(app) as c:  # restart
        app.dependency_overrides[get_extract_llm] = claim_llm
        latest = c.get(f"/api/companies/{company_id}/claims").json()
        assert latest["running"] is None and "server restarted" in latest["failed_run"]["error"]
        started = c.post(f"/api/companies/{company_id}/claim-check", json={})
        assert started.status_code == 202
        c.company_id = company_id
        assert wait_for(c, started.json()["id"])["status"] == "done"  # leave no background job behind
