"""Test helpers for v2: load ClearClaim AI and script FakeLLM replies that behave like a careful model."""

import json
import re
from pathlib import Path

from app.extract import extract_metrics
from app.ingest import ingest_document
from app.llm import FakeLLM
from tests.helpers import chunks_in_prompt

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CLEARCLAIM = "ClearClaim AI"
DECK = "clearclaim_deck_2025-12.pdf"

# What a careful model lists for the December 2025 deck (and the matching sentences of the September deck).
CLAIM_RULES = [
    {"key": "market.problem_rate_pct", "value_raw": "40%",
     "quote": "40% of dental insurance claims are denied on first submission."},
    {"key": "market.customer_count", "value_raw": "400,000",
     "quote": "There are 400,000 independent dental practices in the United States."},
    {"key": "market.price_usd", "value_raw": "$3,600",
     "quote": "At $3,600 per practice per year, our total addressable market is $12B."},
    {"key": "market.size_usd", "value_raw": "$12B",
     "quote": "At $3,600 per practice per year, our total addressable market is $12B."},
    {"key": "traction.paying_customers", "value_raw": "85",
     "quote": "As of November 30, 2025, 85 dental practices pay for ClearClaim."},
    {"key": "product.outcome", "value_raw": "15 hours",
     "quote": "ClearClaim saves front-desk staff 15 hours per week per practice."},
    {"key": "unit.gross_margin_pct", "value_raw": "67-82%",
     "quote": "Gross margin is 67-82%, depending on payer mix and the share of claims that need manual review."},
    {"key": "competition.landscape", "value_raw": None,
     "quote": "No other company offers AI agents for dental insurance claims."},
    {"key": "team.headcount", "value_raw": "14", "quote": "We are a team of 14 full-time employees."},
    {"key": "risk.stated", "value_raw": None,
     "quote": "Payers may change claim submission rules or restrict clearinghouse access, which could lower "
              "first-pass acceptance and increase customer churn."},
    {"key": "finance.opex_annual_usd", "value_raw": "$420,000",
     "quote": "Total annual operating costs for 2025 are $420,000."},
    {"key": "unit.gross_margin_pct", "value_raw": "above 90%", "quote": "Current gross margin: above 90%."},
    {"key": "projection.assumption", "value_raw": None,
     "quote": "Our model assumes 0% customer churn and 98% first-pass claim acceptance through 2029."},
]

# Passages a careful model links to claims. `claim` is a fragment of the claim's quote.
# The last rule is a careless link: a passage on the topic with no number, which the program must drop.
EVIDENCE_RULES = [
    {"claim": "85 dental practices pay", "quote": "85 dental practices were paying for ClearClaim",
     "value_raw": "85", "stance": "supports"},
    {"claim": "85 dental practices pay", "quote": "Reviewed traction: 85 paying practices as of November 30, 2025.",
     "value_raw": "85", "stance": "supports"},
    {"claim": "Gross margin is 67-82%", "quote": "Maya said gross margin is around 70% today",
     "value_raw": "around 70%", "stance": "supports"},
    {"claim": "Current gross margin: above 90%", "quote": "Maya said gross margin is around 70% today",
     "value_raw": "around 70%", "stance": "contradicts"},
    {"claim": "15 hours per week", "quote": "Maya said practices tell them the product saves a lot of front-desk time.",
     "value_raw": None, "stance": "supports"},
]


def claim_extractor(rules=CLAIM_RULES, statement=lambda rule: f"The deck says: {rule['quote']}"):
    """FakeLLM reply for "Extract claims": every rule whose quote is in a chunk of the prompt."""
    def reply(call):
        out = []
        for chunk_id, _label, text in chunks_in_prompt(call["prompt"]):
            flat = " ".join(text.split())
            out += [{"chunk_id": chunk_id, "key": r["key"], "quote": r["quote"], "value_raw": r["value_raw"],
                     "statement": statement(r)} for r in rules if r["quote"] in flat]
        return {"claims": out}
    return reply


CLAIM_LINE = re.compile(r"^(\d+)\. \((\S+)\)(?: \[number: [^\]]*\])? \"(.*)\"$", re.MULTILINE)
PASSAGE = re.compile(r"^  \[chunk_id=(\d+)\] \(.*?\)\n  (.*)$", re.MULTILINE)


def offered(prompt: str) -> tuple[dict[int, tuple[str, str]], dict[int, list[tuple[int, str]]]]:
    """Claims ({index: (key, quote)}) and the passages offered for each claim in a review prompt."""
    claims = {int(i): (key, quote) for i, key, quote in CLAIM_LINE.findall(prompt)}
    passages: dict[int, list[tuple[int, str]]] = {}
    for block in re.split(r"^Claim (\d+):$", prompt.split("Candidate passages per claim:")[1], flags=re.MULTILINE)[1:]:
        if block.strip().isdigit():
            current = int(block)
            passages[current] = []
        else:
            passages[current] = [(int(c), t) for c, t in PASSAGE.findall(block)]
    return claims, passages


def evidence_reviewer(rules=EVIDENCE_RULES, pair_risks=True):
    """FakeLLM reply for "Review evidence": links offered passages by rule, and pairs assumption with risk."""
    def reply(call):
        claims, passages = offered(call["prompt"])
        evidence = []
        for i, (_key, quote) in claims.items():
            for rule in rules:
                if rule["claim"] in quote:
                    for chunk_id, text in passages.get(i, []):
                        if rule["quote"] in text:
                            evidence.append({"claim": i, "chunk_id": chunk_id, "quote": rule["quote"],
                                             "stance": rule["stance"], "value_raw": rule["value_raw"]})
        pairs = []
        if pair_risks:
            assumptions = [i for i, (k, _) in claims.items() if k == "projection.assumption"]
            risks = [i for i, (k, _) in claims.items() if k == "risk.stated"]
            pairs = [{"assumption_claim": a, "risk_claim": r} for a in assumptions for r in risks]
        return {"evidence": evidence, "risk_conflicts": pairs}
    return reply


def claim_llm(extractor=None, reviewer=None) -> FakeLLM:
    """One FakeLLM for a whole claim check: routes each call by the skill section in its system prompt."""
    extractor, reviewer = extractor or claim_extractor(), reviewer or evidence_reviewer()

    def reply(call):
        if "list the key claims" in call["system"]:
            return extractor(call)
        if "check the claims of a pitch deck" in call["system"]:
            return reviewer(call)
        raise AssertionError(f"unexpected model call: {call['system'][:80]}")
    return FakeLLM(default=reply)


def load_clearclaim(conn, stage=("diligence",), company=CLEARCLAIM) -> tuple[int, dict[str, int]]:
    """Load ClearClaim files of the given stages. Returns (company_id, {filename: document_id})."""
    company_id = conn.execute("INSERT INTO companies(name) VALUES (?)", (company,)).lastrowid
    docs = {}
    no_metrics = FakeLLM(default={"metrics": []})
    for m in json.loads((SAMPLES / "manifest_v2.json").read_text()):
        if m["stage"] not in stage:
            continue
        path = SAMPLES / m["path"]
        doc = ingest_document(conn, company_id, path.name, m["doc_type"], m["doc_date"], path.read_bytes())
        extract_metrics(conn, doc["document_id"], no_metrics)
        docs[path.name] = doc["document_id"]
    conn.commit()
    return company_id, docs


def by_quote(claims: list[dict], fragment: str) -> dict:
    found = [c for c in claims if fragment in c["quote"]]
    assert len(found) == 1, f"{len(found)} claims match {fragment!r}"
    return found[0]
