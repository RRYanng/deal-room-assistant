"""Citation check: a quote counts only if it appears verbatim in the cited chunk.

"Verbatim" after whitespace normalization: runs of spaces/newlines become one space,
so a quote copied across a line break still matches. Nothing else is forgiven.
"""

import re
import sqlite3

_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def quote_in_text(quote: str, text: str) -> bool:
    q = normalize_ws(quote)
    return bool(q) and q in normalize_ws(text)


def verify_citation(conn: sqlite3.Connection, company_id: int, chunk_id, quote: str) -> sqlite3.Row | None:
    """Return the chunk row if `quote` is verbatim in chunk `chunk_id` of this company, else None."""
    try:
        chunk_id = int(chunk_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        "SELECT ch.id, ch.document_id, ch.text, ch.loc_label, d.filename, d.doc_type, d.doc_date"
        " FROM chunks ch JOIN documents d ON d.id = ch.document_id"
        " WHERE ch.id = ? AND ch.company_id = ?",
        (chunk_id, company_id),
    ).fetchone()
    if row and quote_in_text(quote, row["text"]):
        return row
    return None
