"""Claim check: what a deck claims, what the evidence says, and a status decided by the program.

One run checks one deck (instructions: skills/claim-check/SKILL.md):
  1. the model lists the deck's claims (verbatim quote, fixed-vocabulary key, number as written);
     the program keeps a claim only if the quote is verbatim in a chunk of that deck, and reads the
     number itself (app.numbers)
  2. program checks inside the deck: one figure stated with two values that do not agree, market
     size arithmetic, operating cost per employee below a floor
  3. internal evidence: hybrid search finds passages in the company's other materials dated near the
     deck; the model says which passages support or contradict a claim; the program checks each
     quote and, for a claim with a number, compares the numbers itself
  4. web research adds external evidence (app.research), when a web search provider is configured
  5. derive_status() sets each claim's status from its evidence
The model never writes a status. Statuses describe evidence, not truth.
"""

import hashlib
import json
import logging
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from app.citations import normalize_ws, quote_in_text, verify_citation
from app.db import connect
from app.llm import LLMClient, LLMError
from app.logs import log_event
from app.numbers import parse_range, ranges_agree
from app.rag import search
from app.skills import skill_prompt

log = logging.getLogger(__name__)

# Fixed vocabulary. Fingerprints, comparisons and (later) memory use these keys, never model wording.
KEYS = {
    "market.customer_count": "number of potential customers in the target market",
    "market.price_usd": "price per customer per year used to size the market",
    "market.size_usd": "total addressable market in dollars",
    "market.problem_rate_pct": "how often the problem happens in the industry, in percent",
    "traction.paying_customers": "number of paying customers",
    "traction.revenue_usd": "revenue, ARR or MRR",
    "product.outcome": "a measurable result the product delivers to customers",
    "unit.gross_margin_pct": "gross margin in percent",
    "team.headcount": "number of full-time employees",
    "finance.opex_annual_usd": "annual operating costs",
    "projection.assumption": "an assumption used in the financial projections",
    "risk.stated": "a risk the company itself acknowledges",
    "competition.landscape": "statements about competitors, or about having none",
    "other": "any other claim worth checking",
}
STATUSES = ("contradicted", "possibly_overstated", "externally_supported", "consistent_with_company_data",
            "unverified")
# Two values of one key in the same deck are compared only for keys that name one figure.
NOT_COMPARED_IN_DECK = {"other", "product.outcome", "projection.assumption", "risk.stated", "competition.landscape"}

TOLERANCE = 0.10          # single values within 10% agree
OVERSTATED_RATIO = 1.5    # a claim at least 1.5x its benchmark is possibly overstated
CONTEXT_WINDOW_DAYS = 60  # other materials count as evidence only if dated this close to the deck
CANDIDATES_PER_CLAIM = 6

# Floor for operating cost per full-time employee. BLS Employer Costs for Employee Compensation,
# December 2025 (released 2026-03-20): private industry employers paid $46.15 per hour worked
# (https://www.bls.gov/news.release/archives/ecec_03202026.htm). x 2,080 hours = about $96,000 a year;
# the floor is half of that. Operating costs also include rent, software and fees, so a company below
# half of the average pay cost alone has very likely misstated its team size or its costs.
COST_PER_EMPLOYEE_FLOOR_USD = 48_000

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "integer"},
            "key": {"type": "string", "enum": list(KEYS)},
            "quote": {"type": "string"},
            "value_raw": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "statement": {"type": "string"},
        },
        "required": ["chunk_id", "key", "quote", "value_raw", "statement"],
        "additionalProperties": False,
    }}},
    "required": ["claims"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "claim": {"type": "integer"},
                "chunk_id": {"type": "integer"},
                "quote": {"type": "string"},
                "stance": {"type": "string", "enum": ["supports", "contradicts"]},
                "value_raw": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["claim", "chunk_id", "quote", "stance", "value_raw"],
            "additionalProperties": False,
        }},
        "risk_conflicts": {"type": "array", "items": {
            "type": "object",
            "properties": {"assumption_claim": {"type": "integer"}, "risk_claim": {"type": "integer"}},
            "required": ["assumption_claim", "risk_claim"],
            "additionalProperties": False,
        }},
    },
    "required": ["evidence", "risk_conflicts"],
    "additionalProperties": False,
}


# --- fingerprints ---------------------------------------------------------------------

def fingerprint(company_id: int, item_type: str, key: str, value_range: tuple[float, float] | None,
                quote: str = "") -> str:
    """Stable id of a flagged item: company + type + fixed key + numbers read by the program.

    Model wording never enters it, so the same file checked twice gives the same fingerprints. A
    claim with no number uses the verbatim source text instead (the document's words, not the model's).
    """
    if value_range is not None:
        detail = "|".join(_num(v) for v in value_range)
    else:
        detail = "q:" + hashlib.sha256(normalize_ws(quote).lower().encode()).hexdigest()[:16]
    return hashlib.sha256(f"{company_id}|{item_type}|{key}|{detail}".encode()).hexdigest()[:24]


def _num(v: float) -> str:
    return "inf" if v == math.inf else "-inf" if v == -math.inf else f"{v:.6g}"


# --- 1. extraction ----------------------------------------------------------------------

def build_extract_prompt(doc: sqlite3.Row, chunks: list[sqlite3.Row]) -> str:
    lines = [f"Company: {doc['company_name']}", f"Deck: {doc['filename']} (date: {doc['doc_date'] or 'unknown'})",
             "", "Chunks:"]
    for ch in chunks:
        lines += [f"[chunk_id={ch['id']}] ({ch['loc_label']})", ch["text"], ""]
    return "\n".join(lines)


def extract_claims(conn: sqlite3.Connection, doc: sqlite3.Row, llm: LLMClient, log_id: str) -> list[dict]:
    """The deck's claims, each checked against its chunk. Raises LLMError on model failure."""
    chunks = conn.execute("SELECT id, text, loc_label FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                          (doc["id"],)).fetchall()
    by_id = {ch["id"]: ch for ch in chunks}
    keys = "\n".join(f"  - {k}: {desc}" for k, desc in KEYS.items())
    proposed = llm.complete_json(skill_prompt("claim-check", "Extract claims", keys=keys),
                                 build_extract_prompt(doc, chunks), EXTRACT_SCHEMA).get("claims", [])
    claims, seen = [], set()
    for item in proposed if isinstance(proposed, list) else []:
        claim, reason = _validate_claim(item, by_id, doc["company_id"])
        if claim is None:
            log_event(conn, log_id, 1, "claim_dropped", input=item, output=reason)
        elif claim["fingerprint"] in seen:
            log_event(conn, log_id, 1, "claim_duplicate", input=item)
        else:
            seen.add(claim["fingerprint"])
            claims.append(claim | {"document_id": doc["id"]})
    return claims


def _validate_claim(item, chunks_by_id: dict, company_id: int) -> tuple[dict | None, str]:
    if not isinstance(item, dict):
        return None, "not an object"
    chunk = chunks_by_id.get(item.get("chunk_id"))
    if chunk is None:
        return None, "chunk_id is not a chunk of this deck"
    quote = normalize_ws(str(item.get("quote", "")))
    if not quote_in_text(quote, chunk["text"]):
        return None, "quote is not verbatim in the chunk"
    key = item.get("key")
    if key not in KEYS:
        return None, f"unknown key {key!r}"
    value_raw = normalize_ws(str(item.get("value_raw") or "")) or None
    value_range = None
    if value_raw:
        if value_raw.lower() not in quote.lower():
            return None, "value_raw is not part of the quote"
        value_range = parse_range(value_raw)
    statement = normalize_ws(str(item.get("statement") or "")) or quote
    return {"chunk_id": chunk["id"], "loc_label": chunk["loc_label"], "key": key, "quote": quote,
            "statement": statement, "value_raw": value_raw, "range": value_range,
            "fingerprint": fingerprint(company_id, "claim", key, value_range, quote)}, ""


# --- 2. program checks inside the deck -----------------------------------------------------

def deck_checks(claims: list[dict]) -> list[dict]:
    """Evidence found by arithmetic and comparison alone. Each item names its claim by index."""
    found = []
    by_key: dict[str, list[int]] = {}
    for i, c in enumerate(claims):
        if c["range"] is not None:
            by_key.setdefault(c["key"], []).append(i)

    # One figure, two values that do not agree (e.g. gross margin "67-82%" and "above 90%").
    for key, indexes in by_key.items():
        if key in NOT_COMPARED_IN_DECK:
            continue
        for i in indexes:
            for j in indexes:
                if i != j and not ranges_agree(claims[i]["range"], claims[j]["range"], TOLERANCE):
                    other = claims[j]
                    found.append(evidence_item(i, "internal", "contradicts", chunk_id=other["chunk_id"],
                                           quote=other["quote"], value_raw=other["value_raw"],
                                           note=f"The same deck also states {other['value_raw']} "
                                                f"({other['loc_label']})."))

    # Market size arithmetic: customers x price should give the stated market size.
    first = {key: claims[idx[0]] for key, idx in by_key.items()}
    count, price, size = (first.get(k) for k in ("market.customer_count", "market.price_usd", "market.size_usd"))
    if count and price and size and _is_point(count) and _is_point(price) and _is_point(size):
        product = count["range"][0] * price["range"][0]
        if not ranges_agree((product, product), size["range"], TOLERANCE):
            found.append(evidence_item(claims.index(size), "calculation", "contradicts",
                                   note=f"{count['value_raw']} x {price['value_raw']} = {_usd(product)} "
                                        f"(calculated), not {size['value_raw']}."))

    # Cost per employee below the floor: the team size or the costs are likely misstated.
    opex, heads = first.get("finance.opex_annual_usd"), first.get("team.headcount")
    if opex and heads and _is_point(opex) and _is_point(heads) and heads["range"][0] > 0:
        per_head = opex["range"][0] / heads["range"][0]
        if per_head < COST_PER_EMPLOYEE_FLOOR_USD:
            note = (f"{opex['value_raw']} / {heads['value_raw']} = {_usd(per_head)} per full-time employee "
                    f"(calculated), below the {_usd(COST_PER_EMPLOYEE_FLOOR_USD)} floor (half the average US "
                    f"employer cost of one worker, BLS, December 2025). The team size may be overstated or "
                    f"the costs understated.")
            for claim in (opex, heads):
                found.append(evidence_item(claims.index(claim), "calculation", "overstates", note=note))
    return found


def _is_point(claim: dict) -> bool:
    lo, hi = claim["range"]
    return lo == hi and math.isfinite(lo)


def _usd(value: float) -> str:
    return f"${value:,.0f}"


def evidence_item(claim: int, source: str, stance: str, note: str, chunk_id=None, web_source_id=None,
              quote=None, value_raw=None) -> dict:
    return {"claim": claim, "source": source, "stance": stance, "chunk_id": chunk_id,
            "web_source_id": web_source_id, "quote": quote, "value_raw": value_raw, "note": note}


# --- 3. internal evidence ---------------------------------------------------------------

def context_documents(conn: sqlite3.Connection, company_id: int, deck: sqlite3.Row) -> dict[int, sqlite3.Row]:
    """The company's other materials dated within CONTEXT_WINDOW_DAYS of the deck.

    The deck itself never counts (a claim cannot support itself), and neither does an undated file
    or one far from the deck: an update a year later may differ for good reason.
    """
    if not deck["doc_date"]:
        return {}
    deck_day = date.fromisoformat(deck["doc_date"])
    rows = conn.execute("SELECT id, filename, doc_type, doc_date FROM documents"
                        " WHERE company_id = ? AND id != ? AND doc_date IS NOT NULL", (company_id, deck["id"]))
    return {r["id"]: r for r in rows
            if abs((date.fromisoformat(r["doc_date"]) - deck_day).days) <= CONTEXT_WINDOW_DAYS}


def find_candidates(conn: sqlite3.Connection, company_id: int, claims: list[dict],
                    docs: dict[int, sqlite3.Row]) -> list[list[dict]]:
    """Per claim, passages from the context documents that hybrid search ranks highest."""
    if not docs:
        return [[] for _ in claims]
    out = []
    for c in claims:
        hits = search(conn, company_id, f"{c['statement']} {c['quote']}", top_k=CANDIDATES_PER_CLAIM * 3)
        out.append([h for h in hits if h["document_id"] in docs][:CANDIDATES_PER_CLAIM])
    return out


def build_review_prompt(claims: list[dict], candidates: list[list[dict]]) -> str:
    lines = ["Claims:"]
    for i, c in enumerate(claims):
        number = f" [number: {c['value_raw']}]" if c["value_raw"] else ""
        lines.append(f"{i}. ({c['key']}){number} \"{c['quote']}\"")
    lines += ["", "Candidate passages per claim:"]
    for i, hits in enumerate(candidates):
        if hits:
            lines.append(f"Claim {i}:")
            for h in hits:
                lines += [f"  [chunk_id={h['chunk_id']}] ({h['loc_label']})", "  " + " ".join(h["text"].split())]
    return "\n".join(lines)


def review_internal_evidence(conn: sqlite3.Connection, company_id: int, claims: list[dict],
                             candidates: list[list[dict]], llm: LLMClient, log_id: str) -> list[dict]:
    """Evidence from the company's own other materials, checked by the program. Raises LLMError."""
    has_pairs = any(c["key"] == "projection.assumption" for c in claims) and \
        any(c["key"] == "risk.stated" for c in claims)
    if not claims or (not any(candidates) and not has_pairs):
        return []
    answer = llm.complete_json(skill_prompt("claim-check", "Review evidence"),
                               build_review_prompt(claims, candidates), REVIEW_SCHEMA)
    found, seen = [], set()
    for item in answer.get("evidence") or []:
        evidence, reason = _validate_internal(conn, company_id, item, claims, candidates)
        if evidence is None:
            log_event(conn, log_id, 2, "evidence_dropped", input=item, output=reason)
        elif (evidence["claim"], evidence["chunk_id"]) not in seen:
            seen.add((evidence["claim"], evidence["chunk_id"]))
            found.append(evidence)
    for pair in answer.get("risk_conflicts") or []:
        a, r = pair.get("assumption_claim"), pair.get("risk_claim")
        if not (valid_index(a, claims) and valid_index(r, claims)) or claims[a]["key"] != "projection.assumption" \
                or claims[r]["key"] != "risk.stated":
            log_event(conn, log_id, 2, "risk_conflict_dropped", input=pair, output="not an assumption and a stated risk")
            continue
        risk = claims[r]
        found.append(evidence_item(a, "internal", "overstates", chunk_id=risk["chunk_id"], quote=risk["quote"],
                               note=f"The deck states this risk ({risk['loc_label']}), but the projection "
                                    f"assumes it away."))
    return found


def valid_index(i, items) -> bool:
    return isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(items)


def _validate_internal(conn, company_id, item, claims, candidates) -> tuple[dict | None, str]:
    if not isinstance(item, dict) or not valid_index(item.get("claim"), claims):
        return None, "unknown claim"
    i = item["claim"]
    hit = next((h for h in candidates[i] if h["chunk_id"] == item.get("chunk_id")), None)
    if hit is None:
        return None, "chunk_id was not offered for this claim"
    row = verify_citation(conn, company_id, hit["chunk_id"], str(item.get("quote", "")))
    if row is None:
        return None, "quote is not verbatim in the passage"
    quote = normalize_ws(item["quote"])
    claim = claims[i]
    where = f"{row['filename']}, {row['loc_label'].replace(row['filename'], '').strip()}".rstrip(", ")
    if claim["range"] is None:  # no number: the stance is the model's reading of a verified quote
        stance = item.get("stance")
        if stance not in ("supports", "contradicts"):
            return None, "unknown stance"
        return evidence_item(i, "internal", stance, chunk_id=row["id"], quote=quote,
                         note=f"{'Consistent with' if stance == 'supports' else 'Contradicted by'} {where}."), ""
    # A claim with a number: the program compares the numbers and decides the stance itself.
    value_raw = normalize_ws(str(item.get("value_raw") or ""))
    if not value_raw or value_raw.lower() not in quote.lower():
        return None, "a claim with a number needs a number in the quote"
    value_range = parse_range(value_raw)
    if value_range is None:
        return None, "value_raw is not a number"
    if ranges_agree(claim["range"], value_range, TOLERANCE):
        return evidence_item(i, "internal", "supports", chunk_id=row["id"], quote=quote, value_raw=value_raw,
                         note=f"Same figure in {where}: {value_raw}."), ""
    return evidence_item(i, "internal", "contradicts", chunk_id=row["id"], quote=quote, value_raw=value_raw,
                     note=f"Different figure in {where}: {value_raw}."), ""


# --- 5. status ----------------------------------------------------------------------------

def derive_status(evidence: list[dict]) -> str:
    """The claim's evidence status, decided by these rules only (highest first)."""
    stances = {(e["source"], e["stance"]) for e in evidence}
    if any(stance == "contradicts" for _, stance in stances):
        return "contradicted"
    if any(stance == "overstates" for _, stance in stances):
        return "possibly_overstated"
    if ("external", "supports") in stances:
        return "externally_supported"
    if ("internal", "supports") in stances:
        return "consistent_with_company_data"
    return "unverified"


# --- the run --------------------------------------------------------------------------------

def latest_deck(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT id FROM documents WHERE company_id = ? AND doc_type = 'deck'"
                        " ORDER BY doc_date IS NULL, doc_date DESC, id DESC LIMIT 1", (company_id,)).fetchone()


def create_run(conn: sqlite3.Connection, company_id: int, document_id: int) -> int:
    deck = conn.execute("SELECT id FROM documents WHERE id = ? AND company_id = ? AND doc_type = 'deck'",
                        (document_id, company_id)).fetchone()
    if not deck:
        raise ValueError(f"Document {document_id} is not a deck of company {company_id}.")
    with conn:
        return conn.execute("INSERT INTO claim_runs(company_id, document_id, status, stage) VALUES (?, ?, 'running',"
                            " 'Queued')", (company_id, document_id)).lastrowid


def run_claim_check(conn: sqlite3.Connection, run_id: int, llm: LLMClient, web=None) -> None:
    """Do the work of a created run and store the result. Failures are stored on the run, not raised."""
    run = conn.execute("SELECT company_id, document_id FROM claim_runs WHERE id = ?", (run_id,)).fetchone()
    company_id, log_id = run["company_id"], f"claims-run{run_id}"
    notes: list[str] = []
    try:
        deck = conn.execute(
            "SELECT d.id, d.company_id, d.filename, d.doc_type, d.doc_date, c.name AS company_name"
            " FROM documents d JOIN companies c ON c.id = d.company_id WHERE d.id = ?", (run["document_id"],)
        ).fetchone()
        _stage(conn, run_id, "Listing the deck's claims")
        claims = extract_claims(conn, deck, llm, log_id)
        evidence = deck_checks(claims)

        _stage(conn, run_id, "Checking claims against the company's other materials")
        docs = context_documents(conn, company_id, deck)
        if not docs:
            notes.append("No other materials dated within 60 days of the deck, so no internal evidence was sought.")
        evidence += review_internal_evidence(conn, company_id, claims, find_candidates(conn, company_id, claims, docs),
                                             llm, log_id)

        if web is None:
            notes.append("Web research was not run: no web search provider is configured.")
        else:
            from app.research import research_claims  # noqa: PLC0415 (research builds on this module)
            _stage(conn, run_id, "Researching the market and competitors on the web")
            found, research_notes = research_claims(conn, run_id, deck, claims, llm, web)
            evidence += found
            notes += research_notes

        _save(conn, run_id, company_id, claims, evidence, notes)
    except LLMError as e:
        _fail(conn, run_id, f"Model call failed: {e}", notes)
    except Exception as e:  # noqa: BLE001 (the run must end in a stored state the page can show)
        log.exception("Claim check %s failed", run_id)
        _fail(conn, run_id, f"Claim check failed ({type(e).__name__}: {e}).", notes)


def _stage(conn: sqlite3.Connection, run_id: int, stage: str) -> None:
    with conn:
        conn.execute("UPDATE claim_runs SET stage = ? WHERE id = ?", (stage, run_id))


def _fail(conn: sqlite3.Connection, run_id: int, error: str, notes: list[str]) -> None:
    with conn:
        conn.execute("UPDATE claim_runs SET status = 'failed', stage = NULL, error = ?, notes_json = ?,"
                     " finished_at = CURRENT_TIMESTAMP WHERE id = ?", (error, json.dumps(notes), run_id))


def _save(conn: sqlite3.Connection, run_id: int, company_id: int, claims: list[dict], evidence: list[dict],
          notes: list[str]) -> None:
    with conn:
        for i, c in enumerate(claims):
            mine = [e for e in evidence if e["claim"] == i]
            lo, hi = c["range"] if c["range"] else (None, None)
            claim_id = conn.execute(
                "INSERT INTO claims(run_id, company_id, document_id, chunk_id, key, statement, quote, value_raw,"
                " value_lo, value_hi, fingerprint, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, company_id, c["document_id"], c["chunk_id"], c["key"], c["statement"], c["quote"],
                 c["value_raw"], _db_num(lo), _db_num(hi), c["fingerprint"], derive_status(mine))).lastrowid
            for e in mine:
                conn.execute(
                    "INSERT INTO evidence(claim_id, company_id, source, stance, chunk_id, web_source_id, quote,"
                    " value_raw, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (claim_id, company_id, e["source"], e["stance"], e["chunk_id"], e["web_source_id"],
                     e["quote"], e["value_raw"], e["note"]))
        conn.execute("UPDATE claim_runs SET status = 'done', stage = NULL, notes_json = ?,"
                     " finished_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(notes), run_id))


def _db_num(v):
    """SQLite REAL cannot hold infinity reliably across drivers; store open ends as NULL."""
    return v if v is not None and math.isfinite(v) else None


# --- background runs ----------------------------------------------------------------------

# One worker: claim checks run one after another and never compete for the database or the model.
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="claim-check")


def start_claim_check(db_path: Path | str, company_id: int, document_id: int, llm: LLMClient, web=None) -> int:
    conn = connect(db_path)
    try:
        run_id = create_run(conn, company_id, document_id)
    finally:
        conn.close()
    _worker.submit(_run_in_background, db_path, run_id, llm, web)
    return run_id


def _run_in_background(db_path, run_id: int, llm: LLMClient, web) -> None:
    conn = connect(db_path)
    try:
        run_claim_check(conn, run_id, llm, web)
    finally:
        conn.close()


# --- reading results ------------------------------------------------------------------------

def get_run(conn: sqlite3.Connection, company_id: int, run_id: int) -> dict | None:
    row = conn.execute("SELECT r.*, d.filename FROM claim_runs r JOIN documents d ON d.id = r.document_id"
                       " WHERE r.id = ? AND r.company_id = ?", (run_id, company_id)).fetchone()
    if not row:
        return None
    run = {k: row[k] for k in ("id", "company_id", "document_id", "filename", "status", "stage", "error",
                               "started_at", "finished_at")}
    return run | {"notes": json.loads(row["notes_json"] or "[]")}


def latest_results(conn: sqlite3.Connection, company_id: int) -> dict:
    """The newest finished run of the company with its claims and evidence, plus any run still going."""
    running = conn.execute("SELECT id FROM claim_runs WHERE company_id = ? AND status = 'running'"
                           " ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
    last = conn.execute("SELECT id FROM claim_runs WHERE company_id = ? AND status != 'running'"
                        " ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
    result = {"running": running and get_run(conn, company_id, running["id"]),
              "run": last and get_run(conn, company_id, last["id"]), "claims": [], "competitors": []}
    if last:
        result["claims"] = run_claims(conn, company_id, last["id"])
        result["competitors"] = run_competitors(conn, company_id, last["id"])
    return result


def run_claims(conn: sqlite3.Connection, company_id: int, run_id: int) -> list[dict]:
    claims = conn.execute(
        "SELECT cl.id, cl.key, cl.statement, cl.quote, cl.value_raw, cl.fingerprint, cl.status, cl.chunk_id,"
        " ch.loc_label, d.filename, d.doc_date FROM claims cl JOIN chunks ch ON ch.id = cl.chunk_id"
        " JOIN documents d ON d.id = cl.document_id WHERE cl.run_id = ? AND cl.company_id = ? ORDER BY cl.id",
        (run_id, company_id)).fetchall()
    evidence = conn.execute(
        "SELECT e.claim_id, e.source, e.stance, e.quote, e.value_raw, e.note, e.chunk_id, ch.loc_label,"
        " d.filename, w.url, w.title FROM evidence e JOIN claims cl ON cl.id = e.claim_id"
        " LEFT JOIN chunks ch ON ch.id = e.chunk_id LEFT JOIN documents d ON d.id = ch.document_id"
        " LEFT JOIN web_sources w ON w.id = e.web_source_id"
        " WHERE cl.run_id = ? AND e.company_id = ? ORDER BY e.id", (run_id, company_id)).fetchall()
    by_claim: dict[int, list[dict]] = {}
    for e in evidence:
        by_claim.setdefault(e["claim_id"], []).append({k: e[k] for k in e.keys() if k != "claim_id"})
    return [dict(c) | {"evidence": by_claim.get(c["id"], [])} for c in claims]


def run_competitors(conn: sqlite3.Connection, company_id: int, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.name, c.description, c.quote, w.url, w.title FROM competitors c"
        " JOIN web_sources w ON w.id = c.web_source_id WHERE c.run_id = ? AND c.company_id = ? ORDER BY c.id",
        (run_id, company_id)).fetchall()
    return [dict(r) for r in rows]
