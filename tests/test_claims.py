"""Claim check on ClearClaim AI: extraction checks, program checks, internal evidence and statuses."""

import math

import pytest

from app.claims import (COST_PER_EMPLOYEE_FLOOR_USD, context_documents, create_run, derive_status, fingerprint,
                        is_spoken, latest_deck, latest_results, run_claim_check)
from app.llm import FakeLLM, LLMError
from app.numbers import parse_range, ranges_agree
from tests.helpers import load_samples
from tests.helpers_v2 import (CLAIM_RULES, DECK, EVIDENCE_RULES, SEP_DECK_RULES, by_quote, claim_extractor, claim_llm,
                              evidence_reviewer, load_clearclaim)


def check(db, company_id, document_id, llm=None, web=None):
    run_id = create_run(db, company_id, document_id)
    run_claim_check(db, run_id, llm or claim_llm(), web)
    return latest_results(db, company_id)


@pytest.fixture
def clearclaim(db):
    company_id, docs = load_clearclaim(db)
    return company_id, docs


@pytest.fixture
def results(db, clearclaim):
    company_id, docs = clearclaim
    return check(db, company_id, docs[DECK])


# --- numbers ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("67-82%", (67, 82)), ("above 90%", (90, math.inf)), ("90%+", (90, math.inf)), ("under 5", (-math.inf, 5)),
    ("85", (85, 85)), ("$12B", (12e9, 12e9)), ("$420,000", (420_000, 420_000)), ("around 70%", (70, 70)),
    ("$1-2M", (1e6, 2e6)), ("15 hours", (15, 15)), ("none", None),
])
def test_parse_range(raw, expected):
    assert parse_range(raw) == expected


@pytest.mark.parametrize("a, b, agree", [
    ("67-82%", "above 90%", False),   # a stated range is used as written
    ("67-82%", "around 70%", True),
    ("above 90%", "around 70%", False),
    ("85", "85", True), ("100", "109", True), ("100", "112", False),  # single values: +/-10%
])
def test_ranges_agree(a, b, agree):
    assert ranges_agree(parse_range(a), parse_range(b)) is agree


# --- planted problems (E1-E6) ---------------------------------------------------------------

def test_e1_gross_margin_stated_two_ways_is_contradicted(results):
    low = by_quote(results["claims"], "Gross margin is 67-82%")
    high = by_quote(results["claims"], "Current gross margin: above 90%")
    assert high["status"] == "contradicted"
    assert low["status"] == "contradicted"  # the deck disagrees with itself; both sides are flagged
    notes = [e["note"] for e in high["evidence"]]
    assert any("67-82%" in n for n in notes)
    # The notes say around 70%: that agrees with 67-82% and not with above 90%.
    assert any(e["stance"] == "supports" and e["filename"] == "clearclaim_meeting_notes.md" for e in low["evidence"])
    assert any(e["stance"] == "contradicts" and e["filename"] == "clearclaim_meeting_notes.md"
               for e in high["evidence"])


def test_e2_market_size_arithmetic_is_contradicted(results):
    size = next(c for c in results["claims"] if c["key"] == "market.size_usd")
    assert size["status"] == "contradicted"
    calc = [e for e in size["evidence"] if e["source"] == "calculation"]
    assert calc and "$1,440,000,000 (calculated)" in calc[0]["note"]


def test_e3_cost_per_employee_below_floor_is_possibly_overstated(results):
    assert COST_PER_EMPLOYEE_FLOOR_USD == 48_651  # $23.39 x 2,080 (BLS, Service occupations)
    for fragment in ("operating costs for 2025 are $420,000", "team of 14 full-time employees"):
        claim = by_quote(results["claims"], fragment)
        assert claim["status"] == "possibly_overstated"
        assert any("$30,000 per full-time employee" in e["note"] and "BLS" in e["note"] for e in claim["evidence"])


def test_e4_projection_that_assumes_away_a_stated_risk_is_possibly_overstated(results):
    claim = by_quote(results["claims"], "Our model assumes 0% customer churn")
    assert claim["status"] == "possibly_overstated"
    [risk] = claim["evidence"]
    assert risk["stance"] == "overstates" and risk["quote"].startswith("Payers may change claim submission rules")


def test_e5_claim_without_evidence_stays_unverified(results):
    """The notes talk about saved time with no number, and quote an industry survey (20 hours a week on
    verification). Neither is evidence about what the product saves."""
    claim = by_quote(results["claims"], "15 hours per week")
    assert claim["status"] == "unverified"
    assert claim["evidence"] == []


def test_e5_industry_survey_quoted_in_the_notes_is_dropped(db, clearclaim):
    """The founder quoted an industry survey (20 hours a week). Labelled "industry", it is dropped,
    so it neither supports nor contradicts "15 hours per week"."""
    company_id, docs = clearclaim
    rules = [{"claim": "15 hours per week", "quote": "front desks spend up to 20 hours a week on insurance verification",
              "value_raw": "20 hours", "stance": "contradicts", "scope": "industry"}]
    out = check(db, company_id, docs[DECK], claim_llm(reviewer=evidence_reviewer(rules)))
    assert by_quote(out["claims"], "15 hours per week")["status"] == "unverified"
    dropped = db.execute("SELECT output_json FROM agent_logs WHERE kind = 'evidence_dropped'").fetchall()
    assert any("industry-level data" in r["output_json"] for r in dropped)


def test_industry_data_the_company_cites_is_not_company_data(results):
    """The live run marked the problem statement "Consistent with company data" from a survey the founder
    quoted. Industry data repeated by the company is shown, but the claim stays Unverified."""
    claim = by_quote(results["claims"], "spend hours every day on the phone with insurers")
    assert claim["status"] == "unverified"
    [e] = claim["evidence"]
    assert e["scope"] == "industry"
    assert e["note"].startswith("Company cites an industry survey (not independently verified)")


def test_e6_paying_practices_show_both_the_notes_and_the_crm(results):
    claim = by_quote(results["claims"], "85 dental practices pay")
    assert claim["status"] == "consistent_with_company_data"
    assert {e["filename"] for e in claim["evidence"]} == {"clearclaim_meeting_notes.md", "clearclaim_crm.csv"}


# --- what the founder says is not evidence -------------------------------------------------------

OUTCOME = "Practices get paid faster and spend less staff time on insurance"
OUTCOME_CLAIM = {"key": "product.outcome", "value_raw": None,
                 "quote": "Practices get paid faster and spend less staff time on insurance; patients get clearer "
                          "cost estimates before treatment."}
SAID_SAVES_TIME = {"claim": OUTCOME, "value_raw": None, "stance": "supports",
                   "quote": "Maya said practices tell them the product saves a lot of front-desk time."}


def test_e15_claim_only_the_founder_backs_is_unverified(db, clearclaim):
    """The live run marked the outcome claim "Consistent with company data" because the meeting notes say
    "Maya said practices tell them the product saves a lot of front-desk time". What the founder says in a
    meeting is not evidence: the item is shown, not counted, and the claim stays Unverified."""
    company_id, docs = clearclaim
    llm = claim_llm(extractor=claim_extractor(CLAIM_RULES + SEP_DECK_RULES + [OUTCOME_CLAIM]),
                    reviewer=evidence_reviewer(EVIDENCE_RULES + [SAID_SAVES_TIME]))
    claim = by_quote(check(db, company_id, docs[DECK], llm)["claims"], OUTCOME)
    assert claim["status"] == "unverified"
    [e] = claim["evidence"]
    assert e["scope"] == "founder" and e["stance"] == "supports"
    assert e["headline"] == "Only supported by the founder's own statement"


def test_e15_founder_statement_is_found_from_the_sentence_not_only_the_quote(results):
    """The quote can leave out "Maya said"; the program reads the whole sentence around it."""
    claim = by_quote(results["claims"], "85 dental practices pay")
    notes = next(e for e in claim["evidence"] if e["filename"] == "clearclaim_meeting_notes.md")
    assert notes["quote"] == "85 dental practices were paying for ClearClaim"
    assert notes["scope"] == "founder"
    assert notes["headline"] == "Founder's own statement (not counted)"


def test_founder_statement_on_a_contradicted_claim_is_not_called_the_only_support(results):
    """Found in the browser walkthrough: "Gross margin is 67-82%" is Contradicted (the deck also says above
    90%), and the founder's "around 70%" was headed "Only supported by the founder's own statement"."""
    claim = by_quote(results["claims"], "Gross margin is 67-82%")
    assert claim["status"] == "contradicted"
    said = next(e for e in claim["evidence"] if e["scope"] == "founder")
    assert said["headline"] == "Founder's own statement (not counted)"


def test_e6_crm_record_still_makes_paying_practices_consistent(results):
    claim = by_quote(results["claims"], "85 dental practices pay")
    assert claim["status"] == "consistent_with_company_data"
    crm = next(e for e in claim["evidence"] if e["filename"] == "clearclaim_crm.csv")
    assert crm["scope"] == "company" and crm["stance"] == "supports"


def test_a_founder_statement_that_contradicts_the_deck_still_counts(results):
    """The rule is about support. The founder saying around 70% while the deck says above 90% is a real
    inconsistency and stays counted."""
    claim = by_quote(results["claims"], "Current gross margin: above 90%")
    said = next(e for e in claim["evidence"] if e["filename"] == "clearclaim_meeting_notes.md")
    assert said["stance"] == "contradicts" and said["scope"] == "company"
    assert claim["status"] == "contradicted"


@pytest.mark.parametrize("quote, text, spoken", [
    ("85 dental practices were paying", "Maya said that as of November 30, 2025, 85 dental practices were paying.", True),
    ("saves time", "Customers told us it saves time.", True),
    ("revenue was $1.2M", "According to the CEO, revenue was $1.2M in 2025.", True),
    ("85 paying practices", "Reviewed traction: 85 paying practices as of November 30, 2025.", False),
    ("cash is $2.1M", "We checked the bank statement. Cash is $2.1M. Maya said burn is flat.", False),
])
def test_founder_statement_detection(quote, text, spoken):
    assert is_spoken(quote, text) is spoken


def test_without_web_research_external_claims_stay_unverified_and_the_run_says_why(results):
    assert by_quote(results["claims"], "400,000 independent dental practices")["status"] == "unverified"
    assert by_quote(results["claims"], "No other company offers")["status"] == "unverified"
    assert any("Web research was not run" in n for n in results["run"]["notes"])


# --- what the program refuses from the model ------------------------------------------------

def test_claims_with_a_non_verbatim_quote_or_number_or_key_are_dropped(db, clearclaim):
    company_id, docs = clearclaim
    bad = [{"key": "traction.paying_customers", "value_raw": "85", "quote": "85 dental practices pay for ClearClaim"},
           {"key": "traction.paying_customers", "value_raw": "95",
            "quote": "As of November 30, 2025, 85 dental practices pay for ClearClaim."},
           {"key": "made.up", "value_raw": None, "quote": "Current gross margin: above 90%."}]

    def reply(call):
        chunk_id = next(int(line.split("=")[1].split("]")[0]) for line in call["prompt"].splitlines()
                        if line.startswith("[chunk_id=") and "p.4" in line)
        paraphrase = {"chunk_id": chunk_id, "key": "traction.paying_customers", "value_raw": "85",
                      "quote": "Eighty-five practices pay.", "statement": "s"}
        return {"claims": [paraphrase] + claim_extractor(bad)(call)["claims"]}

    out = check(db, company_id, docs[DECK], claim_llm(extractor=reply))
    # Only the exact quote with a number from that quote survives (the first rule is a clean substring).
    assert [(c["key"], c["value_raw"]) for c in out["claims"]] == [("traction.paying_customers", "85")]


def test_evidence_with_an_altered_quote_is_dropped(db, clearclaim):
    company_id, docs = clearclaim
    altered = [{"claim": "85 dental practices pay", "quote": "90 dental practices were paying for ClearClaim",
                "value_raw": "90", "stance": "supports"}]
    out = check(db, company_id, docs[DECK], claim_llm(reviewer=evidence_reviewer(altered)))
    claim = by_quote(out["claims"], "85 dental practices pay")
    assert claim["status"] == "unverified" and claim["evidence"] == []


def test_the_program_decides_the_stance_for_numbers(db, clearclaim):
    """A model that calls a different number "supports" does not make it support."""
    company_id, docs = clearclaim
    wrong = [{"claim": "Current gross margin: above 90%", "quote": "Maya said gross margin is around 70% today",
              "value_raw": "around 70%", "stance": "supports"}]
    out = check(db, company_id, docs[DECK], claim_llm(reviewer=evidence_reviewer(wrong)))
    [e] = [e for e in by_quote(out["claims"], "above 90%")["evidence"]
           if e["filename"] == "clearclaim_meeting_notes.md"]
    assert e["stance"] == "contradicts"


def test_evidence_from_another_company_is_refused(db):
    ids = load_samples(db)
    company_id, docs = load_clearclaim(db)
    tidewell_chunk = db.execute("SELECT id FROM chunks WHERE company_id = ? AND text LIKE '%60 paying customers%'",
                                (ids["Tidewell Insurance"],)).fetchone()["id"]

    def reviewer(call):
        return {"evidence": [{"claim": 0, "chunk_id": tidewell_chunk, "quote": "60 paying customers",
                              "stance": "supports", "value_raw": "60"}], "risk_conflicts": []}

    llm = claim_llm(reviewer=reviewer)
    out = check(db, company_id, docs[DECK], llm)
    assert all(e["chunk_id"] != tidewell_chunk for c in out["claims"] for e in c["evidence"])
    # Nothing from the other companies is ever shown to the model.
    prompts = " ".join(call["prompt"] for call in llm.calls)
    assert "Tidewell" not in prompts and "Kitebridge" not in prompts


def test_context_is_only_materials_near_the_deck(db):
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    deck = db.execute("SELECT id, doc_date FROM documents WHERE id = ?", (docs[DECK],)).fetchone()
    names = {d["filename"] for d in context_documents(db, company_id, deck).values()}
    # Not the deck itself, not the earlier deck (77 days before), not the 2026 updates.
    assert names == {"clearclaim_meeting_notes.md", "clearclaim_crm.csv"}


def test_latest_deck_is_the_newest_by_date(db, clearclaim):
    company_id, docs = clearclaim
    assert latest_deck(db, company_id)["id"] == docs[DECK]


# --- fingerprints ---------------------------------------------------------------------------

def test_same_file_checked_twice_gives_identical_fingerprints(db, clearclaim):
    """Model wording differs between the runs; fingerprints use only stable fields."""
    company_id, docs = clearclaim
    first = check(db, company_id, docs[DECK], claim_llm(claim_extractor(statement=lambda r: "Wording A.")))
    second = check(db, company_id, docs[DECK], claim_llm(claim_extractor(statement=lambda r: f"Other {r['key']}")))
    assert first["run"]["id"] != second["run"]["id"]
    assert [c["statement"] for c in first["claims"]] != [c["statement"] for c in second["claims"]]
    assert sorted(c["fingerprint"] for c in first["claims"]) == sorted(c["fingerprint"] for c in second["claims"])
    assert len({c["fingerprint"] for c in first["claims"]}) == len(CLAIM_RULES)


def test_fingerprint_changes_with_the_number_and_the_company():
    base = fingerprint(1, "claim", "unit.gross_margin_pct", (67, 82))
    assert base == fingerprint(1, "claim", "unit.gross_margin_pct", (67.0, 82.0))
    assert base != fingerprint(1, "claim", "unit.gross_margin_pct", (67, 85))
    assert base != fingerprint(2, "claim", "unit.gross_margin_pct", (67, 82))
    assert base != fingerprint(1, "signal", "unit.gross_margin_pct", (67, 82))
    no_number = fingerprint(1, "claim", "risk.stated", None, "Payers may  change rules.")
    assert no_number == fingerprint(1, "claim", "risk.stated", None, "payers may change rules.")


# --- status rules and failures ---------------------------------------------------------------

@pytest.mark.parametrize("evidence, status", [
    ([], "unverified"),
    ([("internal", "supports")], "consistent_with_company_data"),
    ([("internal", "supports"), ("external", "supports")], "externally_supported"),
    ([("external", "supports"), ("calculation", "overstates")], "possibly_overstated"),
    ([("external", "overstates"), ("internal", "contradicts")], "contradicted"),
])
def test_derive_status(evidence, status):
    assert derive_status([{"source": s, "stance": t} for s, t in evidence]) == status


def test_a_failed_model_call_is_stored_as_a_failed_run(db, clearclaim):
    company_id, docs = clearclaim

    def broken(call):
        raise LLMError("bad API key")

    out = check(db, company_id, docs[DECK], FakeLLM(default=broken))
    assert out["run"] is None and out["claims"] == []
    assert out["failed_run"]["status"] == "failed"
    assert "Model call failed: bad API key" in out["failed_run"]["error"]


def test_a_failed_rerun_keeps_the_last_good_results(db, clearclaim):
    company_id, docs = clearclaim
    good = check(db, company_id, docs[DECK])

    def broken(call):
        raise LLMError("bad API key")

    out = check(db, company_id, docs[DECK], FakeLLM(default=broken))
    assert out["run"]["id"] == good["run"]["id"]
    assert len(out["claims"]) == len(good["claims"])
    assert "bad API key" in out["failed_run"]["error"]
    # A later good run clears the failure banner.
    assert check(db, company_id, docs[DECK])["failed_run"] is None


def test_only_a_deck_of_the_company_can_be_checked(db, clearclaim):
    company_id, docs = clearclaim
    with pytest.raises(ValueError):
        create_run(db, company_id, docs["clearclaim_meeting_notes.md"])
    with pytest.raises(ValueError):
        create_run(db, company_id + 99, docs[DECK])
