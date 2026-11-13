"""How claim-check results read on the page: plain labels, categories, one-line conclusions, a summary.

Everything here is written by the program from stored fields, so it is the same on every run and never
adds a fact the evidence does not hold.
"""

import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from app.config import PROJECT_ROOT

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
    "business.pricing": "Pricing",
    "other": "Other claim",
}
CATEGORIES = ("Market", "Financials", "Competition", "Team", "Regulatory")
KEY_CATEGORY = {
    "market.": "Market", "product.": "Market", "traction.": "Financials", "unit.": "Financials",
    "finance.": "Financials", "projection.": "Financials", "business.": "Financials", "competition.": "Competition", "team.": "Team",
    "risk.": "Financials",  # a business risk the deck states; the regulatory checklist is separate
}
ISSUE_STATUSES = ("contradicted", "possibly_overstated")
# Short names for sources that are well known; anything else is shown by its page title or site.
SOURCE_NAMES = {"cms.gov": "CMS", "ada.org": "ADA", "bls.gov": "BLS", "census.gov": "Census", "cdc.gov": "CDC"}
# Order of evidence under a claim: the program's own calculations, the company's files, then the web
# from most to least neutral.
SOURCE_ORDER = {("calculation", None): 0, ("internal", None): 1, ("external", "government/association"): 2,
                ("external", "news"): 3, ("external", "competitor site"): 4, ("external", "other"): 5,
                ("external", None): 5}


# Parts the page can hide (config/ui.toml): name used in ?hide= -> its switch in the file.
HIDEABLE = {"changes": "changes_since_last_deck", "regulatory": "regulatory_checklist"}
UI_PATH = PROJECT_ROOT / "config" / "ui.toml"
# The run note memory writes; it goes with the changes when they are hidden.
MEMORY_NOTE = "Read the earlier deck {filename} once, to compare figures (claims only, no web search)."


def hidden_by_config(path: Path = UI_PATH) -> list[str]:
    """The parts config/ui.toml switches off. Read on every call, so an edit shows on the next page load."""
    with open(path, "rb") as f:
        show = tomllib.load(f).get("show", {})
    return [name for name, switch in HIDEABLE.items() if show.get(switch, True) is False]


def parse_hide(text: str) -> frozenset[str]:
    """?hide=changes,regulatory -> the known part names; anything else is ignored."""
    return frozenset(p.strip() for p in (text or "").split(",")) & HIDEABLE.keys()


def hide_parts(result: dict, hidden: frozenset[str]) -> dict:
    """Leave hidden parts out of a claim-check result (in place), before counts and the summary are built."""
    if "changes" in hidden:
        result["changes"] = []
        start = MEMORY_NOTE.split("{")[0]
        for run in (result.get("run"), result.get("failed_run")):
            if run:
                run["notes"] = [n for n in run["notes"] if not n.startswith(start)]
    if "regulatory" in hidden:
        result["regulatory"] = None
    return result


def visible_summary(summary: dict, hidden: frozenset[str]) -> dict:
    """Without the checklist the Regulatory category is empty, so it is not shown at all."""
    if "regulatory" in hidden:
        summary["categories"] = [c for c in summary["categories"] if c["category"] != "Regulatory"]
    return summary


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
    if e.get("scope") == "founder":  # the claim is Unverified only if nothing else counted
        return ("Only supported by the founder's own statement" if claim.get("status") == "unverified"
                else "Founder's own statement (not counted)")
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


# Where the phrase that names a figure ends: "0% customer churn and 98% ..." -> "0% customer churn".
_PHRASE_END = re.compile(r"[,;:.()]|\s(?:and|or|but|through|until|by|in|for|per|to|with|from|over|at)\s", re.IGNORECASE)


def figure_phrase(quote: str, value: str | None, max_words: int = 4) -> str | None:
    """The figure with the words that follow it in the quote, e.g. "98% first-pass claim acceptance"."""
    at = quote.find(value) if value else -1
    if at < 0:
        return None
    rest = quote[at + len(value):] + " "
    end = _PHRASE_END.search(rest)
    return " ".join([value, *rest[:end.start() if end else len(rest)].split()[:max_words]])


def qualify_labels(claims: list[dict]) -> list[dict]:
    """Claims of one kind from one sentence (the model split it) get their figure in the label."""
    groups: dict[tuple, list[dict]] = {}
    for c in claims:
        groups.setdefault((c["key"], c["quote"]), []).append(c)
    for group in groups.values():
        if len(group) > 1:
            for c in group:
                phrase = figure_phrase(c["quote"], c.get("value_raw"))
                if phrase:
                    c["label"] = f"{c['label']}: {phrase}"
    return claims


def group_claims(claims: list[dict]) -> list[dict]:
    """Claims that read as one row on the page and in the memo (in place). Two kinds: the sides of a
    contradiction inside the company's materials (same key, each cites the other's passage and figure), and
    different claims flagged by one and the same calculation (team size and operating costs). Each member
    gets `group` (the row's id, label and one-line conclusion); a claim on its own gets None. Display only:
    every claim keeps its own status, evidence and fingerprint."""
    parent = list(range(len(claims)))

    def find(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = sorted((find(i), find(j)))
        parent[b] = a  # the row keeps the place and id of its first claim

    by_chunk: dict[int, list[int]] = {}
    for i, c in enumerate(claims):
        by_chunk.setdefault(c["chunk_id"], []).append(i)
    by_calculation: dict[tuple, int] = {}
    for i, c in enumerate(claims):
        if c["key"] == "other":
            continue
        for e in c["evidence"]:
            if e["source"] == "internal" and e["stance"] == "contradicts" and c["status"] == "contradicted":
                for j in by_chunk.get(e.get("chunk_id"), []):
                    other = claims[j]
                    if j != i and (other["key"], other["status"], other["value_raw"]) == (c["key"], c["status"], e.get("value_raw")):
                        union(i, j)
            elif e["source"] == "calculation":
                j = by_calculation.setdefault((c["status"], e["note"]), i)
                if j != i:
                    union(i, j)
    groups: dict[int, list[dict]] = {}
    for i, c in enumerate(claims):
        groups.setdefault(find(i), []).append(c)
    for members in groups.values():
        info = _group_info(members) if len(members) > 1 else None
        for c in members:
            c["group"] = info
    return claims


def _group_info(members: list[dict]) -> dict:
    first = members[0]
    if len({c["key"] for c in members}) == 1 and all(c["value_raw"] for c in members):
        # "67-82% on p.5 ¶2 vs above 90% on p.9 ¶3"; the file name only when the sides are in different files
        one_file = len({c["filename"] for c in members}) == 1
        where = [(c["loc_label"] or "").removeprefix(c["filename"] if one_file else "").strip() for c in members]
        return {"id": first["fingerprint"], "label": label(first["key"]),
                "headline": " vs ".join(f"{c['value_raw']} on {w}" for c, w in zip(members, where))}
    calculation = next((e for e in first["evidence"] if e["source"] == "calculation"), None)
    return {"id": first["fingerprint"], "label": " / ".join(dict.fromkeys(c["label"] for c in members)),
            "headline": calculation["headline"] if calculation else first["evidence"][0]["headline"]}


def claim_rows(claims: list[dict]) -> list[dict]:
    """One row per group (see group_claims), at the place of its first claim. A merged row reads like a claim:
    the first claim's fields with the group's id, label and `headline`, its `members`, and one review state
    for all of them (awaiting review while any member is; a decision applies to every member)."""
    rows, merged = [], {}
    for c in claims:
        group = c.get("group")
        if not group:
            rows.append(c)
        elif group["id"] in merged:
            merged[group["id"]]["members"].append(c)
        else:
            merged[group["id"]] = dict(c, fingerprint=group["id"], label=group["label"], headline=group["headline"],
                                       members=[c])
            rows.append(merged[group["id"]])
    for row in merged.values():
        states = [m.get("review_state") for m in row["members"]]
        if None in states:
            continue
        row["review_state"] = ("awaiting_review" if "awaiting_review" in states
                               else "dismissed" if set(states) == {"dismissed"} else "confirmed")
        row["evidence_changed"] = any(m["evidence_changed"] for m in row["members"])
        row["review"] = next((m["review"] for m in row["members"] if m["review"]), None)
    return rows


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


def build_summary(claims: list[dict], changes: list[dict], regulatory: dict | None = None) -> dict:
    """Issues per category and the three that matter most, from claims and changes not dismissed, and
    regulatory checklist items the materials do not address. Counted per row, as the page shows them."""
    live = [c for c in claim_rows(claims) if c.get("review_state") != "dismissed" and c.get("category")]
    moved = [c for c in changes if c.get("review_state") != "dismissed" and c["kind"] == "changed"]
    counts = {name: {"category": name, "issues": 0, "unverified": 0} for name in CATEGORIES}
    for c in live:
        if c["status"] in ISSUE_STATUSES:
            counts[c["category"]]["issues"] += 1
        elif c["status"] == "unverified":
            counts[c["category"]]["unverified"] += 1
    for c in moved:
        counts[category(c["key"]) or "Market"]["issues"] += 1
    missing = [i for i in (regulatory or {}).get("items", []) if i["status"] == "Not addressed in materials"]
    counts["Regulatory"]["issues"] += len(missing)

    candidates = []
    for c in live:
        if c["status"] in ISSUE_STATUSES and c["evidence"]:
            best = min(c["evidence"], key=lambda e: SOURCE_ORDER.get((e["source"], e.get("source_type")), 5))
            rank = (ISSUE_STATUSES.index(c["status"]), _strength(c), CATEGORIES.index(c["category"]))
            candidates.append((rank, c["key"], {"status": c["status"], "category": c["category"],
                                                "text": f"{c['label']}: {c.get('headline') or best['headline']}.",
                                                "fingerprint": c["fingerprint"]}))
    for c in moved:
        candidates.append(((2, 1, 0), c["key"], {"status": "changed", "category": category(c["key"]) or "Market",
                                                 "text": f"{label(c['key'])} changed since the last deck: {c['note']}",
                                                 "fingerprint": c["fingerprint"]}))
    for i in missing:  # after claims and changes: absence from the files is weaker than a contradiction
        candidates.append(((3, 0, 0), i["id"], {"status": "not_addressed", "category": "Regulatory",
                                                 "text": f"{i['name']}: not addressed in materials.", "fingerprint": i["id"]}))
    top, keys = [], set()
    for _, key, item in sorted(candidates, key=lambda x: x[0]):
        if key not in keys:  # one line per subject (both gross margin claims point at the same problem)
            keys.add(key)
            top.append(item)
        if len(top) == 3:
            break
    return {"categories": list(counts.values()), "top": top}
