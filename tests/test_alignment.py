"""Alignment check: who pays, how the company charges, who benefits; tension points become questions."""

from app.claims import create_run, latest_results, run_claim_check
from app.questions import only_verified_numbers
from tests.helpers_v2 import (BENEFIT, DECK, PRICING, alignment_reader, claim_llm, fake_web, load_clearclaim,
                              research_llm)


def check(db, llm=None):
    company_id, docs = load_clearclaim(db)
    run_claim_check(db, create_run(db, company_id, docs[DECK]), llm or research_llm(), fake_web())
    return latest_results(db, company_id)


def test_payer_pricing_and_beneficiaries_are_quoted(db):
    a = check(db)["alignment"]
    assert [r["quote"] for r in a["pricing"]] == [PRICING]
    assert [r["quote"] for r in a["beneficiary"]] == [BENEFIT]
    assert all(r["loc_label"].endswith("p.10 ¶2") or r["loc_label"].endswith("p.10 ¶3")
               for role in ("payer", "pricing", "beneficiary") for r in a[role])


def test_share_of_recovered_fee_is_a_neutral_question(db):
    [point] = check(db)["alignment"]["points"]
    assert point["label"] == "share_of_recovered" and point["quote"] == PRICING
    assert "common" in point["note"] and "not a problem in themselves" in point["note"]
    for word in ("misaligned", "conflict of interest", "violation", "risky", "problematic"):
        assert word not in point["note"].lower()
    q = point["question"]
    assert q["text"].startswith(f"The deck says: \"{PRICING}\" (p.10 ¶2).") and q["text"].endswith("?")
    assert only_verified_numbers(q["text"], q["sources"])


def test_items_whose_quote_is_not_in_the_deck_or_whose_type_is_unknown_are_dropped(db):
    rules = [("tension", "share_of_recovered", "Practices pay 20% of everything recovered."),
             ("tension", "kickbacks", PRICING), ("role", "payer", PRICING)]
    company_id, docs = load_clearclaim(db, company="Other ClearClaim")
    run_claim_check(db, create_run(db, company_id, docs[DECK]), claim_llm(alignment=alignment_reader(rules)))
    a = latest_results(db, company_id)["alignment"]
    assert a["points"] == [] and [r["quote"] for r in a["payer"]] == [PRICING]


def test_a_deck_without_a_business_model_has_no_points(db):
    out = check(db, research_llm())
    assert out["alignment"]["points"]  # ClearClaim has one
    company_id, docs = load_clearclaim(db, company="Quiet Co")
    run_claim_check(db, create_run(db, company_id, docs[DECK]), claim_llm(alignment=alignment_reader([])))
    a = latest_results(db, company_id)["alignment"]
    assert a == {"payer": [], "pricing": [], "beneficiary": [], "points": []}
