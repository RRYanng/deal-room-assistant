"""Web research for the claim check: market figures and competitors, checked by the program.

Two specialists share one tool, search_web, and the v1 tool loop (app.agent.run_agent):
  - market research (skills/market-research/SKILL.md) checks market and industry figures;
  - competitor research (skills/competitor-research/SKILL.md) lists competitors with sources and
    compares them with the deck's competition claims.
Rules enforced here, not left to the model:
  - the company's name is masked in everything the model reads, and a query containing it is refused
    before it reaches the search engine (the deal stays confidential, and a fictional company would
    find nothing anyway);
  - every page a search returns is stored in web_sources; evidence must cite one of this run's pages
    and quote it verbatim, or it is dropped;
  - numbers are compared by the program (compare_external).
"""

import re
import sqlite3

from app.agent import run_agent
from app.citations import normalize_ws, quote_in_text
from app.claims import COMPANY_OWN_KEYS, OVERSTATED_RATIO, TOLERANCE, evidence_item, valid_index
from app.llm import LLMClient, ToolSpec
from app.logs import log_event
from app.numbers import parse_range, ranges_agree
from app.skills import skill_prompt
from app.web import WebSearchError, WebSearchProvider

MARKET_KEYS = ("market.customer_count", "market.problem_rate_pct", "market.industry_spend_usd")  # public data checks these
MAX_SEARCHES = 12          # per run, both specialists together
RESULTS_PER_SEARCH = 5
CONTENT_SHOWN_CHARS = 6000  # per page shown to the model; the full stored text is used for checks
RESEARCH_STEPS = 10
MASK = "[the company]"
# Words that are part of many company names and say nothing about this one.
GENERIC_NAME_WORDS = {"ai", "inc", "llc", "ltd", "co", "corp", "company", "the", "and", "labs", "lab", "tech",
                      "technology", "technologies", "health", "insurance", "finance", "financial", "capital",
                      "group", "systems", "software", "solutions", "holdings", "hq", "app", "io"}

SEARCH_TOOL = ToolSpec(
    "search_web",
    "Search the public web. Returns pages with source_id, url, title and content. Never include the "
    "company's name in a query.",
    {"type": "object", "properties": {"query": {"type": "string", "description": "What to look for."}},
     "required": ["query"]})


class QueryRejected(ValueError):
    """A query names the company under review."""


# --- the company's name ----------------------------------------------------------------------

def company_terms(name: str) -> list[str]:
    """The full name plus its distinctive words ("ClearClaim AI" -> ["clearclaim ai", "clearclaim"])."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in GENERIC_NAME_WORDS and len(w) >= 3]
    return list(dict.fromkeys([normalize_ws(name).lower(), *words]))


def names_company(text: str, company_name: str) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low) for t in company_terms(company_name))


def check_query(query: str, company_name: str) -> str:
    query = normalize_ws(query)
    if not query:
        raise QueryRejected("The query is empty.")
    if names_company(query, company_name) or MASK in query:
        raise QueryRejected("Queries must not name the company under review. Search for the market or the "
                            "figure itself.")
    return query


def mask_company(text: str, company_name: str) -> str:
    for term in sorted(company_terms(company_name), key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", MASK, text, flags=re.IGNORECASE)
    return text


# --- the search tool ----------------------------------------------------------------------

class WebTool:
    """search_web for one claim run: guards queries, stores every page, keeps a shared budget."""

    def __init__(self, conn: sqlite3.Connection, run_id: int, company_id: int, company_name: str,
                 web: WebSearchProvider):
        self.conn, self.run_id, self.company_id, self.company_name, self.web = conn, run_id, company_id, company_name, web
        self.searches = 0
        self.rejected: list[str] = []
        self.errors: list[str] = []

    def __call__(self, name: str, args: dict) -> list[dict]:
        if name != "search_web":
            raise ValueError(f"unknown tool {name!r}")
        raw = str(args.get("query", ""))
        try:
            query = check_query(raw, self.company_name)
        except QueryRejected:
            self.rejected.append(raw)
            raise
        if self.searches >= MAX_SEARCHES:
            raise ValueError("Search budget for this run is used up. Answer with what you have found.")
        self.searches += 1
        try:
            pages = self.web.search(query, RESULTS_PER_SEARCH)
        except WebSearchError as e:
            self.errors.append(str(e))
            raise
        out = []
        with self.conn:
            for p in pages:
                source_id = self.conn.execute(
                    "INSERT INTO web_sources(company_id, run_id, query, url, title, content) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.company_id, self.run_id, query, p.url, p.title, p.content)).lastrowid
                out.append({"source_id": source_id, "url": p.url, "title": p.title,
                            "content": mask_company(p.content[:CONTENT_SHOWN_CHARS], self.company_name)})
        return out

    def source(self, source_id) -> sqlite3.Row | None:
        """A page this run's searches returned, or None (an invented or foreign source)."""
        if not isinstance(source_id, int) or isinstance(source_id, bool):
            return None
        return self.conn.execute("SELECT id, url, title, content FROM web_sources WHERE id = ? AND run_id = ?"
                                 " AND company_id = ?", (source_id, self.run_id, self.company_id)).fetchone()


def verified_quote(source: sqlite3.Row, quote: str) -> str | None:
    """The quote, if it is verbatim in the stored page (the masked name counts as the real one)."""
    quote = normalize_ws(str(quote or ""))
    return quote if quote and MASK not in quote and quote_in_text(quote, source["content"]) else None


def _site(source: sqlite3.Row) -> str:
    return source["title"] or re.sub(r"^https?://(www\.)?", "", source["url"]).split("/")[0]


# --- number comparison --------------------------------------------------------------------

def compare_external(claim_range: tuple[float, float], public: tuple[float, float], relation: str) -> str | None:
    """Stance of a public figure toward a claimed one, decided by rule. None means no conclusion."""
    claim_low = claim_range[0] if claim_range[0] != float("-inf") else claim_range[1]
    public_high = public[1] if public[1] != float("inf") else public[0]
    if relation == "upper_bound":
        # A ceiling cannot be exceeded at all: above it is contradicted, by any margin. Under it says
        # nothing for or against the claim. The 1.5x rule is only for figures of the same quantity.
        return "contradicts" if claim_low > public_high else None
    if ranges_agree(claim_range, public, TOLERANCE):
        return "supports"
    if claim_low >= OVERSTATED_RATIO * public_high:
        return "overstates"
    return "contradicts"


# --- market research ------------------------------------------------------------------------

def market_research(conn: sqlite3.Connection, run_id: int, deck: sqlite3.Row, claims: list[dict], llm: LLMClient,
                    tool: WebTool) -> list[dict]:
    company_name = deck["company_name"]
    indexes = [i for i, c in enumerate(claims) if c["key"] in MARKET_KEYS and c["range"] is not None]
    if not indexes:
        return []
    prompt = "Claims to check (the company's name is withheld):\n" + "\n".join(
        f"{i}. ({claims[i]['key']}) [number: {claims[i]['value_raw']}] \"{mask_company(claims[i]['quote'], company_name)}\""
        for i in indexes)
    log_id = f"claims-run{run_id}-market"
    run = run_agent(conn, deck["company_id"], prompt, llm, system=skill_prompt("market-research", "Research"),
                    max_steps=RESEARCH_STEPS, run_id=log_id, tools=[SEARCH_TOOL], tool_runner=tool)
    found = []
    for item in (run["final"] or {}).get("evidence") or []:
        evidence, reason = _external_evidence(item, claims, indexes, tool)
        if evidence is None:
            log_event(conn, log_id, 0, "evidence_dropped", input=item, output=reason)
        else:
            found.append(evidence)
    return found


def _external_evidence(item, claims, indexes, tool: WebTool) -> tuple[dict | None, str]:
    if not isinstance(item, dict) or item.get("claim") not in indexes:
        return None, "not one of the claims sent for research"
    if claims[item["claim"]]["key"] in COMPANY_OWN_KEYS:
        return None, "web sources describe the industry, not this company's own results"
    source = tool.source(item.get("source_id"))
    if source is None:
        return None, "source was not returned by a search in this run"
    quote = verified_quote(source, item.get("quote"))
    if quote is None:
        return None, "quote is not verbatim in the stored page"
    value_raw = normalize_ws(str(item.get("value_raw") or ""))
    public = parse_range(value_raw) if value_raw and value_raw.lower() in quote.lower() else None
    if public is None:
        return None, "value_raw is not a number in the quote"
    relation = item.get("relation") if item.get("relation") in ("same_quantity", "upper_bound") else "same_quantity"
    claim = claims[item["claim"]]
    stance = compare_external(claim["range"], public, relation)
    if stance is None:
        return None, "public figure is a ceiling the claim stays under: no conclusion"
    kind = "an upper bound" if relation == "upper_bound" else "public figure"
    return evidence_item(item["claim"], "external", stance, web_source_id=source["id"], quote=quote, value_raw=value_raw,
                     note=f"{_site(source)}: {value_raw} ({kind}); the deck says {claim['value_raw']}."), ""


# --- competitor research ----------------------------------------------------------------------

def competitor_research(conn: sqlite3.Connection, run_id: int, deck: sqlite3.Row, claims: list[dict],
                        llm: LLMClient, tool: WebTool) -> tuple[list[dict], list[dict]]:
    """Returns (competitors kept, evidence for competition claims)."""
    company_name = deck["company_name"]
    about = conn.execute("SELECT text FROM chunks WHERE document_id = ? ORDER BY chunk_index LIMIT 4",
                         (deck["id"],)).fetchall()
    indexes = [i for i, c in enumerate(claims) if c["key"] == "competition.landscape"]
    prompt = "\n".join(
        ["What the deck says the company does (the name is withheld):",
         *(mask_company(" ".join(r["text"].split()), company_name) for r in about), "",
         "Competition claims in the deck:" if indexes else "The deck makes no claims about competition.",
         *(f"{i}. \"{mask_company(claims[i]['quote'], company_name)}\"" for i in indexes)])
    log_id = f"claims-run{run_id}-competitors"
    run = run_agent(conn, deck["company_id"], prompt, llm, system=skill_prompt("competitor-research", "Research"),
                    max_steps=RESEARCH_STEPS, run_id=log_id, tools=[SEARCH_TOOL], tool_runner=tool)
    final = run["final"] or {}

    kept: dict[str, dict] = {}
    for item in final.get("competitors") or []:
        competitor, reason = _competitor(item, company_name, tool)
        if competitor is None:
            log_event(conn, log_id, 0, "competitor_dropped", input=item, output=reason)
        elif competitor["name"].lower() not in kept:
            kept[competitor["name"].lower()] = competitor

    evidence = []
    for item in final.get("comparison") or []:
        if not isinstance(item, dict) or item.get("claim") not in indexes or \
                item.get("stance") not in ("supports", "contradicts"):
            log_event(conn, log_id, 0, "comparison_dropped", input=item, output="unknown claim or stance")
            continue
        named = [kept[n.lower()] for n in item.get("competitors") or [] if isinstance(n, str) and n.lower() in kept]
        if not named:
            log_event(conn, log_id, 0, "comparison_dropped", input=item, output="no verified competitor named")
            continue
        names = ", ".join(c["name"] for c in named)
        first = named[0]
        evidence.append(evidence_item(item["claim"], "external", item["stance"], web_source_id=first["web_source_id"],
                                  quote=first["quote"],
                                  note=f"Competitor research found {len(named)} "
                                       f"{'company' if len(named) == 1 else 'companies'} in this market: {names}."))
    return list(kept.values()), evidence


def _competitor(item, company_name: str, tool: WebTool) -> tuple[dict | None, str]:
    if not isinstance(item, dict):
        return None, "not an object"
    name = normalize_ws(str(item.get("name") or ""))
    if not name:
        return None, "no name"
    if names_company(name, company_name) or MASK in name:
        return None, "the company under review is not its own competitor"
    source = tool.source(item.get("source_id"))
    if source is None:
        return None, "source was not returned by a search in this run"
    quote = verified_quote(source, item.get("quote"))
    if quote is None:
        return None, "quote is not verbatim in the stored page"
    if name.lower() not in quote.lower():
        return None, "the quote does not name the competitor"
    return {"name": name, "description": normalize_ws(str(item.get("description") or "")) or None,
            "web_source_id": source["id"], "quote": quote, "url": source["url"]}, ""


# --- both, for one claim run ------------------------------------------------------------------

def research_claims(conn: sqlite3.Connection, run_id: int, deck: sqlite3.Row, claims: list[dict], llm: LLMClient,
                    web: WebSearchProvider) -> tuple[list[dict], list[str]]:
    """External evidence for the claims, plus notes for the run. Stores the competitor list.

    A failed model call still fails the run (raised); a failed search only adds a note, because the
    internal results are still worth showing.
    """
    tool = WebTool(conn, run_id, deck["company_id"], deck["company_name"], web)
    evidence = market_research(conn, run_id, deck, claims, llm, tool)
    competitors, competition_evidence = competitor_research(conn, run_id, deck, claims, llm, tool)
    evidence += competition_evidence
    with conn:
        for c in competitors:
            conn.execute("INSERT INTO competitors(run_id, company_id, name, description, web_source_id, quote)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (run_id, deck["company_id"], c["name"], c["description"], c["web_source_id"], c["quote"]))
    notes = []
    if tool.rejected:
        notes.append(f"{len(tool.rejected)} search {'query was' if len(tool.rejected) == 1 else 'queries were'} "
                     f"refused because they named the company.")
    if tool.errors:
        notes.append(f"Web search failed {len(tool.errors)} time(s): {tool.errors[-1]}")
    log_event(conn, f"claims-run{run_id}", 0, "research_done",
              output={"searches": tool.searches, "rejected": tool.rejected, "errors": tool.errors,
                      "evidence": len(evidence), "competitors": len(competitors)})
    return evidence, notes


def stored_sources(conn: sqlite3.Connection, company_id: int, run_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, query, url, title FROM web_sources WHERE run_id = ? AND company_id = ? ORDER BY id",
                        (run_id, company_id)).fetchall()
    return [dict(r) for r in rows]

