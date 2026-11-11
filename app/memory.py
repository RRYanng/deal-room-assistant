"""Memory: what each deck of a company claimed, kept per company, to see what changed between decks.

After every claim check, each claim of the checked deck is stored under its fixed key (memory_entries)
with its value, quote, file name and date. An earlier deck of the same company that was never checked
is read once (claim extraction only, no web search) so there is something to compare with.

changes() compares the checked deck with the most recent earlier-dated deck, key by key, by rule:
  - a figure that does not agree with any value of the same key in the earlier deck (10% tolerance for
    single values, stated ranges as written) is a change;
  - market figures should not move between two decks a few months apart, so a change there is
    "changed"; traction, team and costs move with time, so a change there is "updated" (shown, not a flag);
  - claims keyed "other" and claims without a number are not compared.
Every query filters on company_id; nothing is shared between companies.
"""

import hashlib
import math
import sqlite3

from app.claims import TOLERANCE, extract_claims
from app.llm import LLMClient
from app.numbers import ranges_agree
from app.presentation import label
from app.questions import change_question

STABLE_PREFIXES = ("market.", "unit.", "product.")  # should not move between decks: a change is a flag
NOT_COMPARED = {"other", "risk.stated", "competition.landscape", "market.status_quo", "projection.assumption",
                "business.pricing"}


def save_deck(conn: sqlite3.Connection, company_id: int, deck: sqlite3.Row, claims: list[dict]) -> None:
    """Replace what memory holds for this deck with these claims (inside the caller's transaction)."""
    conn.execute("DELETE FROM memory_entries WHERE company_id = ? AND document_id = ?", (company_id, deck["id"]))
    for c in claims:
        lo, hi = c["range"] if c["range"] else (None, None)
        conn.execute(
            "INSERT INTO memory_entries(company_id, document_id, filename, doc_date, key, value_raw, value_lo, value_hi,"
            " quote, loc_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, deck["id"], deck["filename"], deck["doc_date"], c["key"], c["value_raw"], _db(lo), _db(hi),
             c["quote"], c["loc_label"]))


def fill_earlier_decks(conn: sqlite3.Connection, company_id: int, deck: sqlite3.Row, llm: LLMClient,
                       log_id: str) -> list[str]:
    """Read earlier decks of this company that memory does not hold yet. Returns their file names.
    Raises LLMError on model failure."""
    earlier = conn.execute(
        "SELECT d.id, d.company_id, d.filename, d.doc_type, d.doc_date, c.name AS company_name FROM documents d"
        " JOIN companies c ON c.id = d.company_id WHERE d.company_id = ? AND d.doc_type = 'deck' AND d.id != ?"
        " AND d.doc_date IS NOT NULL AND d.doc_date < ? AND NOT EXISTS"
        " (SELECT 1 FROM memory_entries m WHERE m.company_id = d.company_id AND m.document_id = d.id)",
        (company_id, deck["id"], deck["doc_date"] or "")).fetchall()
    read = []
    for doc in earlier:
        claims = extract_claims(conn, doc, llm, log_id)
        with conn:
            save_deck(conn, company_id, doc, claims)
        read.append(doc["filename"])
    return read


def changes(conn: sqlite3.Connection, company_id: int, document_id: int) -> list[dict]:
    """What the deck says differently from the most recent earlier deck held in memory."""
    deck = conn.execute("SELECT doc_date FROM memory_entries WHERE company_id = ? AND document_id = ? LIMIT 1",
                        (company_id, document_id)).fetchone()
    if not deck or not deck["doc_date"]:
        return []
    previous = conn.execute(
        "SELECT filename, doc_date FROM memory_entries WHERE company_id = ? AND doc_date < ?"
        " AND (document_id IS NULL OR document_id != ?) ORDER BY doc_date DESC LIMIT 1",
        (company_id, deck["doc_date"], document_id)).fetchone()
    if not previous:
        return []
    now = _entries(conn, "company_id = ? AND document_id = ?", (company_id, document_id))
    before = _entries(conn, "company_id = ? AND filename = ? AND doc_date = ?",
                      (company_id, previous["filename"], previous["doc_date"]))
    out, seen = [], set()
    for new in now:
        old_values = [o for o in before if o["key"] == new["key"]]
        if new["key"] in NOT_COMPARED or not old_values or any(ranges_agree(new["range"], o["range"], TOLERANCE)
                                                                for o in old_values):
            continue
        old = old_values[0]
        fingerprint = _fingerprint(company_id, new["key"], old["range"], new["range"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        stable = new["key"].startswith(STABLE_PREFIXES)
        out.append({
            "key": new["key"], "label": label(new["key"]), "kind": "changed" if stable else "updated",
            "fingerprint": fingerprint,
            "old": {k: old[k] for k in ("value_raw", "quote", "filename", "doc_date", "loc_label")},
            "new": {k: new[k] for k in ("value_raw", "quote", "filename", "doc_date", "loc_label")},
            "note": f"{old['value_raw']} ({old['filename']}, {old['doc_date']}) -> {new['value_raw']} "
                    f"({new['filename']}, {new['doc_date']})."})
        out[-1]["question"] = change_question(out[-1])
    return out


def _entries(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM memory_entries WHERE {where} AND value_raw IS NOT NULL"
                        " AND (value_lo IS NOT NULL OR value_hi IS NOT NULL) ORDER BY id", params).fetchall()
    return [dict(r) | {"range": (r["value_lo"] if r["value_lo"] is not None else -math.inf,
                                 r["value_hi"] if r["value_hi"] is not None else math.inf)} for r in rows]


def _fingerprint(company_id: int, key: str, old: tuple, new: tuple) -> str:
    raw = "|".join([str(company_id), "drift", key, *(f"{v:.6g}" for v in (*old, *new))])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _db(v):
    return v if v is not None and math.isfinite(v) else None
