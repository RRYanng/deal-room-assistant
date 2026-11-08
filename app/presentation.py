"""How claim-check results read on the page: plain labels, categories, one-line conclusions, a summary.

Everything here is written by the program from stored fields, so it is the same on every run and never
adds a fact the evidence does not hold.
"""

import re
from urllib.parse import urlparse

# Fixed keys -> plain English labels shown instead of the key.
KEY_LABELS = {
    "market.customer_count": "Number of target customers",
    "market.price_usd": "Price per customer",
    "market.size_usd": "Market size",
    "market.problem_rate_pct": "How often the problem happens",
    "market.industry_spend_usd": "Industry spending",
    "market.status_quo": "How customers work today",
    "traction.paying_customers": "Paying customers",
    "traction.revenue_usd": "Revenue",
    "product.outcome": "Product result",
    "unit.gross_margin_pct": "Gross margin",
    "team.headcount": "Team size",
    "finance.opex_annual_usd": "Operating costs",
    "projection.assumption": "Projection assumption",
    "risk.stated": "Risk the deck states",
    "competition.landscape": "Competitors",
    "other": "Other claim",
}
CATEGORIES = ("Market", "Financials", "Competition", "Team", "Regulatory")
KEY_CATEGORY = {
    "market.": "Market", "product.": "Market", "traction.": "Financials", "unit.": "Financials",
    "finance.": "Financials", "projection.": "Financials", "competition.": "Competition", "team.": "Team",
    "risk.": "Regulatory",
}
ISSUE_STATUSES = ("contradicted", "possibly_overstated")
# Short names for sources that are well known; anything else is shown by its page title or site.
SOURCE_NAMES = {"cms.gov": "CMS", "ada.org": "ADA", "bls.gov": "BLS", "census.gov": "Census", "cdc.gov": "CDC"}
# Order of evidence under a claim: the program's own calculations, the company's files, then the web
# from most to least neutral.
SOURCE_ORDER = {("calculation", None): 0, ("internal", None): 1, ("external", "government/association"): 2,
                ("external", "news"): 3, ("external", "competitor site"): 4, ("external", "other"): 5,
                ("external", None): 5}


def label(key: str) -> str:
    return KEY_LABELS.get(key, key)


def category(key: str) -> str | None:
    """None for claims keyed "other": they are not counted in the summary."""
    return next((c for prefix, c in KEY_CATEGORY.items() if key.startswith(prefix)), None)


def short_source(url: str | None, title: str | None) -> str:
    host = (urlparse(url or "").hostname or "").removeprefix("www.")
    for domain, name in SOURCE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    if title:
        return title if len(title) <= 40 else title[:37].rstrip() + "..."
    return host or "Web source"


def evidence_headline(e: dict, claim: dict) -> str:
    """One line with the conclusion of one piece of evidence, e.g. "ADA: 205,088 (upper bound); deck says 400,000"."""
    deck = claim.get("value_raw")
    if e.get("scope") == "industry":
        return "Company cites an industry survey (not independently verified)"
    if e["source"] == "calculation":
        return e["note"].replace(" (calculated)", "").split(": the ")[0].rstrip(".")
    if e["source"] == "external":
        if e.get("value_raw"):
            bound = " (upper bound)" if "upper bound" in e["note"] else ""
            return f"{short_source(e.get('url'), e.get('title'))}: {e['value_raw']}{bound}; deck says {deck}"
        found = re.match(r"Competitor research found (\d+) compan\w+ in this market: (.*)\.$", e["note"])
        if found:
            names = [n.strip() for n in found.group(2).split(",")]
            more = f" and {len(names) - 3} more" if len(names) > 3 else ""
            return f"{found.group(1)} competitors found: {', '.join(names[:3])}{more}"
        return e["note"].rstrip(".")
    # Company materials
    if e["stance"] == "overstates":
        return "Assumes away a risk the deck itself states"
    where = _doc_name(e, claim)
    if where == "Same deck" and e.get("value_raw"):
        page = (e.get("loc_label") or "").replace(e.get("filename") or "", "").strip()
        return f"Deck says {deck} here and {e['value_raw']} on {page}"
    if e.get("value_raw"):
        return f"{where}: {e['value_raw']}; deck says {deck}"
    return f"{'Consistent with' if e['stance'] == 'supports' else 'Contradicted by'} {where.lower()}"


def _doc_name(e: dict, claim: dict) -> str:
    filename = e.get("filename") or ""
    if filename and filename == claim.get("filename"):
        return "Same deck"
    if filename.endswith(".csv"):
        return "CRM"
    if "notes" in filename:
        return "Meeting notes"
    return filename or "Company file"


def decorate(claim: dict) -> dict:
    """Add label, category and a headline per evidence item; order evidence from most to least neutral."""
    claim["label"], claim["category"] = label(claim["key"]), category(claim["key"])
    for e in claim["evidence"]:
        e["headline"] = evidence_headline(e, claim)
    claim["evidence"].sort(key=lambda e: SOURCE_ORDER.get((e["source"], e.get("source_type")), 5))
    return claim


def _strength(item: dict) -> int:
    """Lower is stronger: a calculation or an official source beats company files and other web pages."""
    kinds = {(e["source"], e.get("source_type")) for e in item.get("evidence", [])}
    if ("calculation", None) in kinds or ("external", "government/association") in kinds:
        return 0
    if any(source == "internal" for source, _ in kinds):
        return 1
    return 2


def build_summary(claims: list[dict], changes: list[dict]) -> dict:
    """Issues per category and the three that matter most, from claims and changes not dismissed."""
    live = [c for c in claims if c.get("review_state") != "dismissed" and c.get("category")]
    moved = [c for c in changes if c.get("review_state") != "dismissed" and c["kind"] == "changed"]
    counts = {name: {"category": name, "issues": 0, "unverified": 0} for name in CATEGORIES}
    for c in live:
        if c["status"] in ISSUE_STATUSES:
            counts[c["category"]]["issues"] += 1
        elif c["status"] == "unverified":
            counts[c["category"]]["unverified"] += 1
    for c in moved:
        counts[category(c["key"]) or "Market"]["issues"] += 1

    candidates = []
    for c in live:
        if c["status"] in ISSUE_STATUSES and c["evidence"]:
            best = min(c["evidence"], key=lambda e: SOURCE_ORDER.get((e["source"], e.get("source_type")), 5))
            rank = (ISSUE_STATUSES.index(c["status"]), _strength(c), CATEGORIES.index(c["category"]))
            candidates.append((rank, c["key"], {"status": c["status"], "category": c["category"],
                                                "text": f"{c['label']}: {best['headline']}.", "fingerprint": c["fingerprint"]}))
    for c in moved:
        candidates.append(((2, 1, 0), c["key"], {"status": "changed", "category": category(c["key"]) or "Market",
                                                 "text": f"{label(c['key'])} changed since the last deck: {c['note']}",
                                                 "fingerprint": c["fingerprint"]}))
    top, keys = [], set()
    for _, key, item in sorted(candidates, key=lambda x: x[0]):
        if key not in keys:  # one line per subject (both gross margin claims point at the same problem)
            keys.add(key)
            top.append(item)
        if len(top) == 3:
            break
    return {"categories": list(counts.values()), "top": top}
