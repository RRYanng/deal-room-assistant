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
    {"key": "other", "value_raw": None,
     "quote": "Front-desk staff at dental practices spend hours every day on the phone with insurers."},
    {"key": "market.industry_spend_usd", "value_raw": "$400 billion",
     "quote": "Americans spend $400 billion a year on dental care."},
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
    # Keyed as competition the way the live model did; it says how practices work today, not about rivals.
    {"key": "competition.landscape", "value_raw": None,
     "quote": "Today practices rely on manual work or outsourced billing services."},
    {"key": "team.headcount", "value_raw": "14", "quote": "We are a team of 14 full-time employees."},
    {"key": "risk.stated", "value_raw": None,
     "quote": "Payers may change claim submission rules or restrict clearinghouse access, which could lower "
              "first-pass acceptance and increase customer churn."},
    {"key": "finance.opex_annual_usd", "value_raw": "$420,000",
     "quote": "Total annual operating costs for 2025 are $420,000."},
    {"key": "unit.gross_margin_pct", "value_raw": "above 90%", "quote": "Current gross margin: above 90%."},
    {"key": "projection.assumption", "value_raw": None,
     "quote": "Our model assumes 0% customer churn and 98% first-pass claim acceptance through 2029."},
    {"key": "business.pricing", "value_raw": "8%",
     "quote": "Practices pay a monthly subscription per location, plus 8% of the amounts ClearClaim recovers on "
              "previously denied claims."},
]

# The September 2025 deck, read once by memory. (Its "$400 billion" sentence matches a rule above.)
SEP_DECK_RULES = [
    {"key": "market.customer_count", "value_raw": "250,000",
     "quote": "There are 250,000 independent dental practices in the United States."},
    {"key": "market.price_usd", "value_raw": "$3,600",
     "quote": "At $3,600 per practice per year, our total addressable market is $900M."},
    {"key": "market.size_usd", "value_raw": "$900M",
     "quote": "At $3,600 per practice per year, our total addressable market is $900M."},
    {"key": "traction.paying_customers", "value_raw": "40",
     "quote": "As of August 31, 2025, 40 dental practices pay for ClearClaim."},
    {"key": "team.headcount", "value_raw": "9", "quote": "We are a team of 9 full-time employees."},
]

# Passages a careful model links to claims. `claim` is a fragment of the claim's quote.
# The last two rules are traps the program must drop: a passage on the topic with no number, and an
# industry survey the founder quoted (labelled scope "industry"), linked to the product's own result.
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
    {"claim": "15 hours per week", "quote": "front desks spend up to 20 hours a week on insurance verification",
     "value_raw": "20 hours", "stance": "contradicts", "scope": "industry"},
    # The live run linked the same survey to the deck's problem statement.
    {"claim": "spend hours every day on the phone", "value_raw": None, "stance": "supports", "scope": "industry",
     "quote": "Maya cited an industry survey that says front desks spend up to 20 hours a week on insurance verification."},
]


def claim_extractor(rules=CLAIM_RULES + SEP_DECK_RULES, statement=lambda rule: f"The deck says: {rule['quote']}"):
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
                                             "stance": rule["stance"], "value_raw": rule["value_raw"],
                                             "scope": rule.get("scope", "company")})
        pairs = []
        if pair_risks:
            assumptions = [i for i, (k, _) in claims.items() if k == "projection.assumption"]
            risks = [i for i, (k, _) in claims.items() if k == "risk.stated"]
            pairs = [{"assumption_claim": a, "risk_claim": r} for a in assumptions for r in risks]
        return {"evidence": evidence, "risk_conflicts": pairs}
    return reply


PRICING = ("Practices pay a monthly subscription per location, plus 8% of the amounts ClearClaim recovers on "
           "previously denied claims.")
BENEFIT = ("Practices get paid faster and spend less staff time on insurance; patients get clearer cost estimates "
           "before treatment.")
ALIGNMENT_RULES = [("role", "payer", PRICING), ("role", "pricing", PRICING), ("role", "beneficiary", BENEFIT),
                   ("tension", "share_of_recovered", PRICING)]


def alignment_reader(rules=ALIGNMENT_RULES):
    """FakeLLM reply for "Read the business model": every rule whose quote is in a chunk of the deck."""
    def reply(call):
        out = {"roles": [], "tensions": []}
        for chunk_id, _label, text in chunks_in_prompt(call["prompt"]):
            flat = " ".join(text.split())
            for kind, label, quote in rules:
                if quote in flat:
                    key = "role" if kind == "role" else "type"
                    out["roles" if kind == "role" else "tensions"].append({key: label, "chunk_id": chunk_id, "quote": quote})
        return out
    return reply


def claim_llm(extractor=None, reviewer=None, alignment=None) -> FakeLLM:
    """One FakeLLM for a whole claim check: routes each call by the skill section in its system prompt."""
    extractor, reviewer = extractor or claim_extractor(), reviewer or evidence_reviewer()
    alignment = alignment or alignment_reader()

    def reply(call):
        if "who pays the company" in call["system"]:
            return alignment(call)
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


# --- web research ---------------------------------------------------------------------------
# Market pages: real URLs with the one sentence each page was checked to contain (samples/company_c/SOURCES.md).
# Competitor pages: fictional companies on example.com, so tests never make claims about real ones.

from app.web import FakeWebSearch, WebResult  # noqa: E402

ADA = WebResult("https://www.ada.org/resources/research/health-policy-institute/dentist-workforce",
                "The dentist workforce",
                "Dentist workforce data. As of 2025, there are 205,088 professionally active dentists in the U.S. "
                "Supply is projected to grow.")
CMS = WebResult("https://www.cms.gov/files/document/highlights.pdf", "National Health Expenditures 2024 Highlights",
                "Dental Services (4 percent share): Spending for dental services increased 6.6 percent in 2024 to "
                "$189.2 billion following similar growth of 6.4 percent in 2023.")
INDUSTRY_HOURS = WebResult("https://survey.example.com/front-desk", "Front desk survey",
                           "Dental front desks spend up to 20 hours a week on insurance verification.")
MOLARFLOW = WebResult("https://molarflow.example.com/about", "Molarflow",
                      "Molarflow uses AI agents to verify dental insurance and submit claims for dental offices.")
TOOTHLEDGER = WebResult("https://news.example.com/toothledger-seed", "Toothledger raises seed",
                        "Toothledger automates dental insurance verification with AI voice agents that call payers. "
                        "ClearClaim AI was not mentioned.")


def web_pages(query: str) -> list[WebResult]:
    q = query.lower()
    if "dentist" in q or "practices" in q:
        return [ADA]
    if "spending" in q or "expenditure" in q:
        return [CMS, INDUSTRY_HOURS]
    if "competitor" in q or "software" in q or "startup" in q or "automation" in q:
        return [MOLARFLOW, TOOTHLEDGER]
    return []


def fake_web() -> FakeWebSearch:
    return FakeWebSearch(web_pages)


MARKET_QUERIES = ["number of dentists in the US", "US national spending on dental services"]
MARKET_RULES = [  # claim fragment -> what a careful model cites
    {"claim": "400,000 independent dental practices", "url": ADA.url, "relation": "upper_bound",
     "quote": "As of 2025, there are 205,088 professionally active dentists in the U.S.", "value_raw": "205,088"},
    {"claim": "$400 billion a year on dental care", "url": CMS.url, "relation": "same_quantity",
     "quote": "Spending for dental services increased 6.6 percent in 2024 to $189.2 billion",
     "value_raw": "$189.2 billion"},
]
COMPETITOR_QUERIES = ["AI dental insurance verification startup", "dental claims automation software"]
COMPETITOR_RULES = [
    {"name": "Molarflow", "description": "AI agents for dental insurance and claims.",
     "quote": "Molarflow uses AI agents to verify dental insurance and submit claims for dental offices."},
    {"name": "Toothledger", "description": "AI voice agents that verify dental insurance.",
     "quote": "Toothledger automates dental insurance verification with AI voice agents that call payers."},
]


def pages_returned(call) -> list[dict]:
    """Every page the tools returned in the latest turn of a research loop."""
    pages = []
    for r in call["messages"][-1].get("tool_results", []):
        out = json.loads(r["content"])
        if isinstance(out, list):
            pages += out
    return pages


def research_loop(queries, answer):
    """FakeLLM chat reply for a research loop: search once with `queries`, then answer from the pages."""
    def reply(call):
        if len(call["messages"]) == 1:
            return FakeLLM.tool_call("search_web", {"query": queries[0]},
                                     *[("search_web", {"query": q}) for q in queries[1:]])
        return FakeLLM.text(json.dumps(answer(call, pages_returned(call))))
    return reply


def market_answer(rules=MARKET_RULES):
    def answer(call, pages):
        claims = {int(i): quote for i, _k, quote in CLAIM_LINE.findall(call["messages"][0]["content"])}
        evidence = []
        for i, quote in claims.items():
            for rule in rules:
                page = next((p for p in pages if p["url"] == rule["url"]), None)
                if rule["claim"] in quote and page:
                    evidence.append({"claim": i, "source_id": page["source_id"], "quote": rule["quote"],
                                     "value_raw": rule["value_raw"], "relation": rule["relation"]})
        return {"evidence": evidence, "open_items": []}
    return answer


def competitor_answer(rules=COMPETITOR_RULES):
    def answer(call, pages):
        competitors = []
        for rule in rules:
            page = next((p for p in pages if rule["quote"] in p["content"]), None)
            if page:
                competitors.append({"name": rule["name"], "description": rule["description"],
                                    "source_id": page["source_id"], "quote": rule["quote"]})
        claims = [int(i) for i in re.findall(r"^(\d+)\. \"", call["messages"][0]["content"], re.MULTILINE)]
        comparison = [{"claim": i, "stance": "contradicts", "competitors": [c["name"] for c in competitors]}
                      for i in claims]
        return {"sector": "AI insurance verification and claims automation for dental practices",
                "competitors": competitors, "comparison": comparison, "open_items": []}
    return answer


def research_llm(extractor=None, reviewer=None, market=None, competitors=None) -> FakeLLM:
    """claim_llm plus the two research loops."""
    base = claim_llm(extractor, reviewer).default
    market = market or research_loop(MARKET_QUERIES, market_answer())
    competitors = competitors or research_loop(COMPETITOR_QUERIES, competitor_answer())

    def reply(call):
        if call["method"] == "chat" and "market and industry figures" in call["system"]:
            return market(call)
        if call["method"] == "chat" and "map the competitors" in call["system"]:
            return competitors(call)
        return base(call)
    return FakeLLM(default=reply)


# --- portfolio monitoring ---------------------------------------------------------------------
# What a careful model extracts from the three quarterly updates (the meeting notes give nothing).

TEAM = [("Maya Chen", "CEO"), ("Daniel Okafor", "CTO"), ("Priya Raman", "Head of Sales")]
MONITOR_RULES = [
    {"list": "period", "period_end": "2026-03-31", "quote": "As of March 31, 2026, we had $4.4M in cash."},
    {"list": "metrics", "kpi": "cash", "value_raw": "$4.4M", "quote": "As of March 31, 2026, we had $4.4M in cash."},
    {"list": "metrics", "kpi": "monthly_net_burn", "value_raw": "$200,000",
     "quote": "Net burn averaged $200,000 per month in Q1."},
    {"list": "metrics", "kpi": "revenue", "value_raw": "$310,000",
     "quote": "Revenue for Q1 2026 was $310,000, up 15% from Q4 2025."},
    *[{"list": "people", "name": n, "role": r, "status": "present",
       "quote": "Team: Maya Chen (CEO), Daniel Okafor (CTO) and Priya Raman (Head of Sales) lead the company."}
      for n, r in TEAM],
    {"list": "milestones", "name": "Bluecrest Dental Plan integration", "target_date": "2026-05-31", "status": "planned",
     "quote": "Milestone: direct integration with Bluecrest Dental Plan, target date May 31, 2026."},

    {"list": "period", "period_end": "2026-06-30", "quote": "As of June 30, 2026, we had $3.3M in cash."},
    {"list": "metrics", "kpi": "cash", "value_raw": "$3.3M", "quote": "As of June 30, 2026, we had $3.3M in cash."},
    {"list": "metrics", "kpi": "monthly_net_burn", "value_raw": "$300,000",
     "quote": "Net burn averaged $300,000 per month in Q2, as we added two engineers."},
    {"list": "metrics", "kpi": "revenue", "value_raw": "$357,000",
     "quote": "Revenue for Q2 2026 was $357,000, up 15% from Q1 2026."},
    {"list": "milestones", "name": "Bluecrest Dental Plan integration", "target_date": "2026-08-31", "status": "delayed",
     "quote": "Milestone: the Bluecrest Dental Plan integration is now expected on August 31, 2026 "
              "(previously May 31, 2026)."},

    {"list": "period", "period_end": "2026-09-30", "quote": "As of September 30, 2026, we had $2.1M in cash."},
    {"list": "metrics", "kpi": "cash", "value_raw": "$2.1M", "quote": "As of September 30, 2026, we had $2.1M in cash."},
    {"list": "metrics", "kpi": "monthly_net_burn", "value_raw": "$300,000",
     "quote": "Net burn averaged $300,000 per month in Q3."},
    {"list": "metrics", "kpi": "revenue", "value_raw": "$364,000",
     "quote": "Revenue for Q3 2026 was $364,000, up 2% from Q2 2026."},
    {"list": "people", "name": "Daniel Okafor", "role": "CTO", "status": "departed",
     "quote": "Daniel Okafor, our CTO, left the company in August."},
    *[{"list": "people", "name": n, "role": r, "status": "present",
       "quote": "Team: Maya Chen (CEO) and Priya Raman (Head of Sales) lead the company."} for n, r in TEAM if n != "Daniel Okafor"],
    {"list": "milestones", "name": "Bluecrest Dental Plan integration", "target_date": "2026-11-30", "status": "delayed",
     "quote": "Milestone: the Bluecrest Dental Plan integration is now expected on November 30, 2026 "
              "(previously August 31, 2026)."},
]
# The Q2 team sentence repeats Q1's; one rule per person matches both files.


def kpi_extractor(rules=MONITOR_RULES):
    """FakeLLM reply for "Extract KPIs": every rule whose quote is in a chunk of the file in the prompt."""
    def reply(call):
        out = {"period_end": None, "period_chunk_id": None, "period_quote": None,
               "metrics": [], "people": [], "milestones": []}
        for chunk_id, _label, text in chunks_in_prompt(call["prompt"]):
            flat = " ".join(text.split())
            for r in rules:
                if r["quote"] not in flat:
                    continue
                fields = {k: v for k, v in r.items() if k != "list"} | {"chunk_id": chunk_id}
                if r["list"] == "period":
                    out.update(period_end=r["period_end"], period_chunk_id=chunk_id, period_quote=r["quote"])
                else:
                    out[r["list"]].append(fields)
        return out
    return reply


def monitor_llm(rules=MONITOR_RULES) -> FakeLLM:
    extractor = kpi_extractor(rules)

    def reply(call):
        if "investor update from a startup" in call["system"]:
            return extractor(call)
        raise AssertionError(f"unexpected model call: {call['system'][:80]}")
    return FakeLLM(default=reply)
