"""Web research: market figures and competitors (E7-E10), the search guard, and the providers."""

import json

import httpx
import pytest

from app.claims import create_run, latest_results, run_claim_check
from app.config import get_settings
from app.numbers import parse_range
from app.research import QueryRejected, check_query, compare_external, mask_company
from app.web import FakeWebSearch, WebResult, WebSearchError, get_web_search
from app.web.tavily import TavilyProvider
from tests.helpers_v2 import (ADA, CMS, INDUSTRY_HOURS, COMPETITOR_QUERIES, DECK, MARKET_QUERIES, by_quote,
                              competitor_answer, fake_web, load_clearclaim, market_answer, research_llm,
                              research_loop)


def check(db, llm=None, web=None):
    company_id, docs = load_clearclaim(db)
    run_id = create_run(db, company_id, docs[DECK])
    run_claim_check(db, run_id, llm or research_llm(), web or fake_web())
    return latest_results(db, company_id)


@pytest.fixture
def results(db):
    return check(db)


# --- E7-E9: planted claims checked against public sources -----------------------------------

def test_e7_practice_count_above_the_number_of_dentists_is_contradicted(results):
    """205,088 dentists is a ceiling on the number of practices: exceeding it at all is contradicted."""
    claim = by_quote(results["claims"], "400,000 independent dental practices")
    assert claim["status"] == "contradicted"
    [e] = [e for e in claim["evidence"] if e["source"] == "external"]
    assert e["url"] == ADA.url and e["stance"] == "contradicts"
    assert "205,088" in e["quote"] and "upper bound" in e["note"]


def test_e8_dental_spending_is_possibly_overstated_against_a_government_source(results):
    claim = by_quote(results["claims"], "$400 billion a year on dental care")
    assert claim["status"] == "possibly_overstated"
    [e] = [e for e in claim["evidence"] if e["source"] == "external"]
    assert e["url"] == CMS.url and e["value_raw"] == "$189.2 billion" and e["stance"] == "overstates"


def test_e9_competitor_research_lists_competitors_with_links_and_contradicts_the_deck(results):
    assert [(c["name"], c["url"].startswith("https://")) for c in results["competitors"]] == \
        [("Molarflow", True), ("Toothledger", True)]
    claim = by_quote(results["claims"], "No other company offers AI agents")
    assert claim["status"] == "contradicted"
    [e] = claim["evidence"]
    assert e["source"] == "external" and "Molarflow, Toothledger" in e["note"] and e["url"]


def test_a_matching_public_figure_externally_supports_a_claim(db):
    page = WebResult("https://spending.example.com/dental", "Dental spending",
                     "Americans spent $380 billion on dental care in the survey year.")
    rules = [{"claim": "$400 billion a year on dental care", "url": page.url, "relation": "same_quantity",
              "quote": "Americans spent $380 billion on dental care in the survey year.", "value_raw": "$380 billion"}]
    out = check(db, research_llm(market=research_loop(MARKET_QUERIES, market_answer(rules))),
                FakeWebSearch(lambda q: [page]))
    claim = by_quote(out["claims"], "$400 billion a year on dental care")
    assert claim["status"] == "externally_supported"
    assert [e["url"] for e in claim["evidence"]] == [page.url]


@pytest.mark.parametrize("claim, public, relation, stance", [
    ("400,000", "205,088", "upper_bound", "contradicts"),  # above a ceiling, by any margin
    ("210,000", "205,088", "upper_bound", "contradicts"),
    ("205,088", "205,088", "upper_bound", None),
    ("150,000", "205,088", "upper_bound", None),           # under a ceiling: no conclusion
    ("40%", "19.3%", "same_quantity", "overstates"),
    ("21%", "19.3%", "same_quantity", "supports"),
    ("25%", "19.3%", "same_quantity", "contradicts"),
    ("10%", "19.3%", "same_quantity", "contradicts"),   # understated is not overstated
])
def test_compare_external(claim, public, relation, stance):
    assert compare_external(parse_range(claim), parse_range(public), relation) == stance


# --- E10: sources must be real, and the company is never searched for ------------------------

def test_e10_invented_source_or_altered_quote_is_dropped(db):
    def answer(call, pages):
        real = pages[0]["source_id"]
        return {"evidence": [
            {"claim": 1, "source_id": 9999, "quote": "There are 150,000 practices.", "value_raw": "150,000",
             "relation": "same_quantity"},
            {"claim": 1, "source_id": real, "quote": "As of 2025, there are 105,088 professionally active dentists",
             "value_raw": "105,088", "relation": "upper_bound"},
            {"claim": 1, "source_id": real, "quote": "As of 2025, there are 205,088 professionally active dentists",
             "value_raw": "305,088", "relation": "upper_bound"},
        ], "open_items": []}

    out = check(db, research_llm(market=research_loop(MARKET_QUERIES, answer)))
    claim = by_quote(out["claims"], "400,000 independent dental practices")
    assert claim["status"] == "unverified" and claim["evidence"] == []
    dropped = db.execute("SELECT output_json FROM agent_logs WHERE run_id LIKE '%-market'"
                         " AND kind = 'evidence_dropped'").fetchall()
    assert len(dropped) == 3


def test_e10_query_naming_the_company_is_refused_before_the_search(db):
    web = fake_web()
    out = check(db, research_llm(competitors=research_loop(["ClearClaim AI competitors", *COMPETITOR_QUERIES],
                                                           competitor_answer())), web)
    assert all("clearclaim" not in q.lower() for q in web.queries)
    assert web.queries  # the other queries still ran
    assert any("refused because they named the company" in n for n in out["run"]["notes"])


def test_e10_the_model_never_sees_the_company_name_in_research(db):
    llm = research_llm()
    check(db, llm)
    research_calls = [c for c in llm.calls if c["method"] == "chat"]
    assert research_calls
    seen = json.dumps([c["messages"] for c in research_calls])
    assert "ClearClaim" not in seen and "[the company]" in seen


def test_e10_competitor_that_is_the_company_or_not_in_its_quote_is_dropped(db):
    rules = [{"name": "ClearClaim", "description": "x",
              "quote": "Toothledger automates dental insurance verification with AI voice agents that call payers."},
             {"name": "Bitewise", "description": "x",
              "quote": "Molarflow uses AI agents to verify dental insurance and submit claims for dental offices."}]
    out = check(db, research_llm(competitors=research_loop(COMPETITOR_QUERIES, competitor_answer(rules))))
    assert out["competitors"] == []
    assert by_quote(out["claims"], "No other company offers")["status"] == "unverified"


@pytest.mark.parametrize("query", ["ClearClaim AI pricing", "clearclaim competitors", "CLEARCLAIM", "[the company]"])
def test_check_query_refuses_the_company_name(query):
    with pytest.raises(QueryRejected):
        check_query(query, "ClearClaim AI")


@pytest.mark.parametrize("query", ["AI dental claims startups", "clear claim dental spending", "number of dentists US"])
def test_check_query_allows_market_queries(query):
    assert check_query(query, "ClearClaim AI") == query


def test_generic_words_of_a_name_are_not_refused():
    assert check_query("small business insurance market size", "Tidewell Insurance")
    with pytest.raises(QueryRejected):
        check_query("tidewell reviews", "Tidewell Insurance")


def test_mask_company():
    assert mask_company("ClearClaim connects to ClearClaim AI's payers.", "ClearClaim AI") == \
        "[the company] connects to [the company]'s payers."


def test_failed_search_is_a_note_and_internal_results_stay(db):
    class Down(FakeWebSearch):
        def search(self, query, max_results=5):
            raise WebSearchError("Tavily rate limit or monthly quota reached.")

    out = check(db, web=Down())
    assert out["run"]["status"] == "done"
    assert any("Web search failed" in n and "quota" in n for n in out["run"]["notes"])
    assert by_quote(out["claims"], "85 dental practices pay")["status"] == "consistent_with_company_data"


def test_every_returned_page_is_stored_with_its_query(db):
    out = check(db)
    rows = db.execute("SELECT query, url FROM web_sources WHERE run_id = ?", (out["run"]["id"],)).fetchall()
    assert {r["url"] for r in rows} >= {ADA.url, CMS.url}
    assert all(r["query"] for r in rows)


# --- providers ------------------------------------------------------------------------------

def tavily(handler):
    return TavilyProvider("test-key-123", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_tavily_reads_page_text_and_sends_the_key_as_a_header():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key-123"
        body = json.loads(request.content)
        assert body["query"] == "dental practices" and body["include_raw_content"] is True
        return httpx.Response(200, json={"results": [
            {"url": "https://a.example", "title": "A", "content": "snippet", "raw_content": "full page text"},
            {"url": "https://b.example", "title": "B", "content": "only a snippet", "raw_content": None},
            {"url": "", "title": "no url", "content": "dropped"}]})

    pages = tavily(handler).search("dental practices")
    assert [(p.url, p.content) for p in pages] == [("https://a.example", "full page text"),
                                                   ("https://b.example", "only a snippet")]


@pytest.mark.parametrize("status, message", [(401, "rejected the API key"), (429, "quota"), (500, "HTTP 500")])
def test_tavily_errors_never_show_the_key(status, message):
    with pytest.raises(WebSearchError) as e:
        tavily(lambda request: httpx.Response(status, json={"detail": "x"})).search("q")
    assert message in str(e.value) and "test-key-123" not in str(e.value)


def test_no_provider_without_a_tavily_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    assert get_web_search(get_settings()) is None
    monkeypatch.setenv("TAVILY_API_KEY", "abc")
    assert isinstance(get_web_search(get_settings()), TavilyProvider)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "")
    assert get_web_search(get_settings()) is None


def test_research_steps_use_their_skill_files(db):
    llm = research_llm()
    check(db, llm)
    systems = {c["system"].split("\n")[0] for c in llm.calls if c["method"] == "chat"}
    assert any("market and industry figures" in s for s in systems)
    assert any("map the competitors" in s for s in systems)


def test_unused_model_calls_are_not_made_without_market_or_competition_claims(db):
    """Nothing to research for market figures: the market loop is skipped, competitor research still runs."""
    from tests.helpers_v2 import CLAIM_RULES, claim_extractor
    rules = [r for r in CLAIM_RULES if not r["key"].startswith("market.")]
    llm = research_llm(extractor=claim_extractor(rules))
    check(db, llm)
    chats = [c["system"] for c in llm.calls if c["method"] == "chat"]
    assert not any("market and industry figures" in s for s in chats)
    assert any("map the competitors" in s for s in chats)


# --- industry data is not evidence about the company's own results ---------------------------

def test_e5_web_page_with_an_industry_figure_cannot_support_a_product_outcome(db):
    """Even if a researcher cites an industry page for "15 hours per week", the program drops it."""
    from app.research import _external_evidence

    class Tool:
        def source(self, source_id):
            return {"id": 1, "url": INDUSTRY_HOURS.url, "title": INDUSTRY_HOURS.title, "content": INDUSTRY_HOURS.content}

    claims = [{"key": "product.outcome", "range": (15.0, 15.0), "value_raw": "15 hours"}]
    item = {"claim": 0, "source_id": 1, "quote": INDUSTRY_HOURS.content, "value_raw": "20 hours",
            "relation": "same_quantity"}
    evidence, reason = _external_evidence(item, claims, [0], Tool())
    assert evidence is None and "not this company's own results" in reason


def test_e5_product_outcome_stays_unverified_with_web_research_on(results):
    claim = by_quote(results["claims"], "15 hours per week")
    assert claim["status"] == "unverified" and claim["evidence"] == []
