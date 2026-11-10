"""Web research for the claim check: market figures and competitors, checked by the program.

Two specialists share one tool, search_web, and the v1 tool loop (app.agent.run_agent):
  - market research (skills/market-research/SKILL.md) checks market and industry figures;
  - competitor research (skills/competitor-research/SKILL.md) lists competitors with sources and
    compares them with the deck's competition claims.
Rules enforced here, not left to the model:
  - the company's name is masked in everything the model reads, and a query containing it is refused
    before it reaches the search engine (the deal stays confidential, and a fictional company would
    find nothing anyway);
  - market research searches government and industry-association sites first (config/sources.toml)
    and the whole web only when they return nothing, or when the model repeats a query it already
    searched there; competitor research searches the whole web;
  - every page a search returns is stored in web_sources; evidence must cite one of this run's pages
    and quote it verbatim, or it is dropped;
  - numbers are compared by the program (compare_external), and only exact ones: a hedged public
    figure ("over 200,000", "about 135,000", "nearly $190 billion") gives no conclusion.
"""

import re
import sqlite3
import tomllib
from functools import lru_cache, partial
from pathlib import Path
from urllib.parse import urlparse

from app.agent import run_agent
from app.citations import normalize_ws, quote_in_text
from app.claims import COMPANY_OWN_KEYS, OVERSTATED_RATIO, TOLERANCE, evidence_item, valid_index
from app.config import PROJECT_ROOT
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

# Words in product and company names that say nothing about which brand it is.
GENERIC_PRODUCT_WORDS = GENERIC_NAME_WORDS | {"dental", "dentistry", "insurance", "verification", "eligibility", "claims",
                                              "claim", "billing", "agent", "agents", "platform", "service", "services",
                                              "solution", "health", "care"}
# A competitor list can only contradict (or support) a claim that asserts something about competitors.
COMPETITION_ASSERTION = re.compile(
    r"\b(no (other|one|company|companies|competitors?|direct)|nobody|none|only|first|unique(ly)?|unlike|"
    r"fast(er|est)|cheap(er|est)|best|better than|lead(ing|er)|#1|number one|competitors?|competition|rivals?)\b",
    re.IGNORECASE)
SOURCES_PATH = PROJECT_ROOT / "config" / "sources.toml"
# Words that make a figure approximate or open-ended. A figure with one of them is not compared.
_HEDGES = (r"over|more than|above|at least|upwards of|in excess of|about|around|approximately|approx\.?|roughly|"
           r"nearly|almost|close to|some|under|less than|fewer than|below|up to|at most")
HEDGE_BEFORE = re.compile(rf"(?:\b(?:{_HEDGES})|~)\s*$", re.IGNORECASE)
HEDGE_IN_VALUE = re.compile(rf"^(?:(?:{_HEDGES})\b|~)|(?:\+|\bplus)$", re.IGNORECASE)
EXACT_FIGURES = ("When a page gives both an exact figure and a hedged one (\"over\", \"about\", \"more than\", "
                 "\"nearly\"), cite the exact figure; a hedged figure alone gives no conclusion.")
NEWS_DOMAINS = {"reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "nytimes.com", "cnbc.com", "forbes.com",
                "fortune.com", "axios.com", "statnews.com", "fiercehealthcare.com", "healthcaredive.com",
                "modernhealthcare.com", "beckersdental.com", "beckershospitalreview.com", "drbicuspid.com",
                "dentistrytoday.com", "techcrunch.com", "businessinsider.com", "news.example.com"}
SOURCE_TYPES = ("government/association", "news", "competitor site", "other")

SEARCH_TOOL = ToolSpec(
    "search_web",
    "Search the public web. Returns pages with source_id, url, title and content. Never include the "
    "company's name in a query.",
    {"type": "object", "properties": {"query": {"type": "string", "description": "What to look for."}},
     "required": ["query"]})
MARKET_SEARCH_TOOL = ToolSpec(
    "search_web",
    "Search the public web. Searches government and industry-association sites first, and the whole web if "
    "they return nothing. Returns pages with source_id, url, title, content and searched (which of the two "
    "it was). Never include the company's name in a query.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to look for."},
        "whole_web": {"type": "boolean", "description": "Repeat a query you already searched, on the whole web, "
                      "when the government and association pages did not have the figure."}},
     "required": ["query"]})
AUTHORITATIVE, WHOLE_WEB = "government and industry-association sites", "whole web"


class QueryRejected(ValueError):
    """A query names the company under review."""


@lru_cache
def authoritative_domains(path: Path = SOURCES_PATH) -> tuple[str, ...]:
    with open(path, "rb") as f:
        domains = tomllib.load(f)["authoritative_domains"]
    return tuple(dict.fromkeys(d.strip().lower() for d in domains if d.strip()))


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
    """search_web for one claim run: guards queries, stores every page, keeps a shared budget.

    Called with `first=` domains (market research), a query searches those domains first and the whole
    web only if they return nothing; `whole_web` widens a query already searched on them. One model query
    is one unit of the budget, even when it takes two searches.
    """

    def __init__(self, conn: sqlite3.Connection, run_id: int, company_id: int, company_name: str,
                 web: WebSearchProvider):
        self.conn, self.run_id, self.company_id, self.company_name, self.web = conn, run_id, company_id, company_name, web
        self.searches = 0
        self.rejected: list[str] = []
        self.errors: list[str] = []
        self.searched_first: set[str] = set()  # queries already searched on the `first` domains
        self.scopes: list[str] = []            # where each search went, for the run log

    def __call__(self, name: str, args: dict, first: tuple[str, ...] = ()) -> list[dict]:
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
        widen = args.get("whole_web") is True and query.lower() in self.searched_first
        pages, searched = [], WHOLE_WEB
        try:
            if first and not widen:
                pages, searched = self.web.search(query, RESULTS_PER_SEARCH, include_domains=list(first)), AUTHORITATIVE
                self.searched_first.add(query.lower())
                self.scopes.append(AUTHORITATIVE)
            if not pages:
                pages, searched = self.web.search(query, RESULTS_PER_SEARCH), WHOLE_WEB
                self.scopes.append(WHOLE_WEB)
        except WebSearchError as e:
            self.errors.append(str(e))
            raise
        out = []
        with self.conn:
            for p in pages:
                source_id = self.conn.execute(
                    "INSERT INTO web_sources(company_id, run_id, query, url, title, content) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.company_id, self.run_id, query, p.url, p.title, p.content)).lastrowid
                out.append({"source_id": source_id, "url": p.url, "title": p.title, "searched": searched,
                            "content": mask_company(p.content[:CONTENT_SHOWN_CHARS], self.company_name)})
        return out

    def source(self, source_id) -> sqlite3.Row | None:
        """A page this run's searches returned, or None (an invented or foreign source)."""
        if not isinstance(source_id, int) or isinstance(source_id, bool):
            return None
        return self.conn.execute("SELECT id, url, title, content FROM web_sources WHERE id = ? AND run_id = ?"
                                 " AND company_id = ?", (source_id, self.run_id, self.company_id)).fetchone()


def is_hedged(quote: str, value_raw: str) -> bool:
    """True if the figure is approximate or open-ended, in value_raw or in the words just before it."""
    value, text = normalize_ws(value_raw), normalize_ws(quote)
    if HEDGE_IN_VALUE.search(value):
        return True
    at = text.lower().find(value.lower())
    return at >= 0 and (bool(HEDGE_BEFORE.search(text[:at])) or text[at + len(value):].startswith("+"))


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
    if public[1] == float("inf"):
        # "Over 200,000" has no upper end, so it can never show that a claim is too high (or cap anything);
        # it only contradicts a claim of the same quantity below its stated minimum.
        return "contradicts" if relation == "same_quantity" and claim_range[1] < public[0] else None
    public_high = public[1]
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
    prompt = f"{EXACT_FIGURES}\n\nClaims to check (the company's name is withheld):\n" + "\n".join(
        f"{i}. ({claims[i]['key']}) [number: {claims[i]['value_raw']}] \"{mask_company(claims[i]['quote'], company_name)}\""
        for i in indexes)
    log_id = f"claims-run{run_id}-market"
    run = run_agent(conn, deck["company_id"], prompt, llm, system=skill_prompt("market-research", "Research"),
                    max_steps=RESEARCH_STEPS, run_id=log_id, tools=[MARKET_SEARCH_TOOL],
                    tool_runner=partial(tool, first=authoritative_domains()))
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
    if is_hedged(quote, value_raw):
        return None, "the public figure is hedged (over, about, nearly...): no conclusion; cite an exact figure"
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
    # Only claims that assert something about competitors can be checked against a competitor list:
    # "no other company offers this" can, "practices rely on manual work today" cannot.
    indexes = [i for i, c in enumerate(claims)
               if c["key"] == "competition.landscape" and COMPETITION_ASSERTION.search(c["quote"])]
    prompt = "\n".join(
        ["What the deck says the company does (the name is withheld):",
         *(mask_company(" ".join(r["text"].split()), company_name) for r in about), "",
         "Competition claims in the deck:" if indexes else "The deck makes no claims about competition.",
         *(f"{i}. \"{mask_company(claims[i]['quote'], company_name)}\"" for i in indexes)])
    log_id = f"claims-run{run_id}-competitors"
    run = run_agent(conn, deck["company_id"], prompt, llm, system=skill_prompt("competitor-research", "Research"),
                    max_steps=RESEARCH_STEPS, run_id=log_id, tools=[SEARCH_TOOL], tool_runner=tool)
    final = run["final"] or {}

    found: dict[str, dict] = {}
    for item in final.get("competitors") or []:
        competitor, reason = _competitor(item, company_name, tool)
        if competitor is None:
            log_event(conn, log_id, 0, "competitor_dropped", input=item, output=reason)
        elif competitor["key"] not in found:
            found[competitor["key"]] = competitor
    # A competitor's own site may describe that competitor, but it is not neutral evidence about others.
    kept: dict[str, dict] = {}
    for key, c in found.items():
        owner = site_owner(c["url"], list(found.values()))
        if owner and owner != key:
            log_event(conn, log_id, 0, "competitor_dropped", input=c,
                      output=f"source is the site of another competitor ({found[owner]['name']})")
        else:
            kept[key] = c

    evidence = []
    for item in final.get("comparison") or []:
        if not isinstance(item, dict) or item.get("claim") not in indexes or \
                item.get("stance") not in ("supports", "contradicts"):
            log_event(conn, log_id, 0, "comparison_dropped", input=item, output="unknown claim or stance")
            continue
        named = [kept[normalize_name(n)] for n in item.get("competitors") or []
                 if isinstance(n, str) and normalize_name(n) in kept]
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


def normalize_name(text: str) -> str:
    """Lowercase, possessives and punctuation removed: "Pearl’s Precheck" -> "pearl precheck"."""
    text = text.lower().replace("\u2019", "'")
    text = re.sub(r"'s\b", "", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def brand_words(name: str) -> list[str]:
    """The words of a name that identify the brand ("Curve Dental Eligibility+" -> ["curve"])."""
    words = normalize_name(name).split()
    return [w for w in words if w not in GENERIC_PRODUCT_WORDS and len(w) >= 3] or words


def quote_names(quote: str, name: str) -> bool:
    """True if the quote names the competitor: its full name, or one of its brand words, after normalizing."""
    text = f" {normalize_name(quote)} "
    full = normalize_name(name)
    return bool(full) and (f" {full} " in text or any(f" {w} " in text for w in brand_words(name)))


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_owner(url: str, competitors: list[dict]) -> str | None:
    """The competitor whose own site this is, judged by a brand word in the host name."""
    label = host_of(url).rsplit(".", 1)[0]
    for c in competitors:
        if any(len(w) >= 4 and w in label for w in brand_words(c["name"])):
            return c["key"]
    return None


def source_type(url: str, competitors: list[dict]) -> str:
    host = host_of(url)
    domains = {host, ".".join(host.split(".")[-2:])}
    if host.endswith(".gov") or ".gov." in host or host.endswith(".mil") or domains & set(authoritative_domains()):
        return "government/association"
    if site_owner(url, competitors):
        return "competitor site"
    if domains & NEWS_DOMAINS:
        return "news"
    return "other"


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
    if not quote_names(quote, name):
        return None, "the quote does not name the competitor"
    return {"key": normalize_name(name), "name": name,
            "description": normalize_ws(str(item.get("description") or "")) or None,
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
        for page in conn.execute("SELECT id, url FROM web_sources WHERE run_id = ?", (run_id,)).fetchall():
            conn.execute("UPDATE web_sources SET source_type = ? WHERE id = ?",
                         (source_type(page["url"], competitors), page["id"]))
    notes = []
    if tool.rejected:
        notes.append(f"{len(tool.rejected)} search {'query was' if len(tool.rejected) == 1 else 'queries were'} "
                     f"refused because they named the company.")
    if tool.errors:
        notes.append(f"Web search failed {len(tool.errors)} time(s): {tool.errors[-1]}")
    log_event(conn, f"claims-run{run_id}", 0, "research_done",
              output={"searches": tool.searches, "authoritative_searches": tool.scopes.count(AUTHORITATIVE),
                      "whole_web_searches": tool.scopes.count(WHOLE_WEB), "rejected": tool.rejected, "errors": tool.errors,
                      "evidence": len(evidence), "competitors": len(competitors)})
    return evidence, notes


def stored_sources(conn: sqlite3.Connection, company_id: int, run_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, query, url, title FROM web_sources WHERE run_id = ? AND company_id = ? ORDER BY id",
                        (run_id, company_id)).fetchall()
    return [dict(r) for r in rows]

