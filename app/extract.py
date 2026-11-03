"""Metric extraction: one model call per document, then program-side validation.

The model proposes metrics (chunk_id, metric, value_raw, as_of_date, quote, ...).
The program then decides what is kept:
  - the chunk must belong to this document;
  - the quote must appear verbatim in that chunk, and value_raw inside the quote;
  - value is computed from value_raw by app.numbers (not taken from the model);
  - as_of_date is kept only if that date is actually written in the chunk;
  - company, document and location come from the database, never from the model.
"""

import re
import sqlite3
from datetime import date

from app.citations import normalize_ws, quote_in_text
from app.llm import LLMClient
from app.logs import log_event
from app.numbers import currency_of, normalize_number
from app.rag import iso_dates

METRICS = ("registered_customers", "paying_customers", "revenue", "funding_raised", "other")
COUNT_METRICS = ("registered_customers", "paying_customers")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

SYSTEM_PROMPT = """You extract key business metrics from investment materials about one company.

Metric definitions (never mix them up):
- registered_customers: businesses/users that signed up or registered, whether or not they pay.
- paying_customers: customers that pay (paid plan, active paid policy, active paid contract).
- revenue: money earned (revenue, ARR, MRR, GMV is NOT revenue). Only if explicitly stated.
- funding_raised: investment money the company has raised.
- other: any other explicit quantitative KPI; give it a short metric_label.

Rules:
- Only extract numbers that are explicitly stated. Never compute, estimate or infer.
- quote: copy the shortest complete sentence or clause containing the number, character for
  character from the chunk. Do not paraphrase, fix typos, or join text from different chunks.
- chunk_id: the id of the chunk the quote was copied from.
- value_raw: the number exactly as written, without the noun (e.g. "100", "$1.2M", "sixty").
- as_of_date: YYYY-MM-DD only if the same chunk states the date the number refers to
  (e.g. "As of June 30, 2026" -> "2026-06-30"). Otherwise null. Do not use the document date.
- unit: e.g. "customers", "USD", "%", or null. currency: ISO code like "USD" or null.
- If there are no metrics, return an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "integer"},
                    "metric": {"type": "string", "enum": list(METRICS)},
                    "metric_label": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "value_raw": {"type": "string"},
                    "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "as_of_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "quote": {"type": "string"},
                },
                "required": ["chunk_id", "metric", "metric_label", "value_raw", "unit", "currency",
                             "as_of_date", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["metrics"],
    "additionalProperties": False,
}


def build_prompt(doc: sqlite3.Row, chunks: list[sqlite3.Row]) -> str:
    lines = [
        f"Company: {doc['company_name']}",
        f"Document: {doc['filename']} (type: {doc['doc_type']}, document date: {doc['doc_date'] or 'unknown'})",
        "",
        "Chunks:",
    ]
    for ch in chunks:
        lines += [f"[chunk_id={ch['id']}] ({ch['loc_label']})", ch["text"], ""]
    return "\n".join(lines)


def extract_metrics(conn: sqlite3.Connection, document_id: int, llm: LLMClient) -> dict:
    """Run extraction for one document and replace its stored metrics. Raises LLMError on model failure."""
    doc = conn.execute(
        "SELECT d.id, d.company_id, d.filename, d.doc_type, d.doc_date, c.name AS company_name"
        " FROM documents d JOIN companies c ON c.id = d.company_id WHERE d.id = ?", (document_id,)
    ).fetchone()
    if not doc:
        raise ValueError(f"Document {document_id} not found.")
    chunks = conn.execute("SELECT id, text, loc_label FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                          (document_id,)).fetchall()
    by_id = {ch["id"]: ch for ch in chunks}

    proposed = llm.complete_json(SYSTEM_PROMPT, build_prompt(doc, chunks), SCHEMA).get("metrics", [])

    run_id = f"extract-doc{document_id}"
    kept, dropped = [], []
    for item in proposed:
        record, reason = _validate(item, by_id)
        if record is None:
            dropped.append({"item": item, "reason": reason})
            log_event(conn, run_id, 0, "metric_dropped", input=item, output=reason)
        else:
            kept.append(record)

    with conn:
        conn.execute("DELETE FROM metrics WHERE document_id = ?", (document_id,))
        for r in kept:
            conn.execute(
                "INSERT INTO metrics(company_id, document_id, chunk_id, metric, metric_label, value,"
                " value_raw, unit, currency, as_of_date, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc["company_id"], document_id, r["chunk_id"], r["metric"], r["metric_label"], r["value"],
                 r["value_raw"], r["unit"], r["currency"], r["as_of_date"], r["quote"]),
            )
    log_event(conn, run_id, 0, "extraction_done", input={"proposed": len(proposed)},
              output={"kept": len(kept), "dropped": len(dropped)})
    return {"status": "ok", "proposed": len(proposed), "kept": len(kept), "dropped": dropped}


def _validate(item: dict, chunks_by_id: dict) -> tuple[dict | None, str]:
    chunk = chunks_by_id.get(item.get("chunk_id"))
    if chunk is None:
        return None, "chunk_id is not a chunk of this document"
    quote = normalize_ws(item.get("quote", ""))
    if not quote_in_text(quote, chunk["text"]):
        return None, "quote is not verbatim in the chunk"
    value_raw = normalize_ws(item.get("value_raw", ""))
    if not value_raw or value_raw.lower() not in quote.lower():
        return None, "value_raw is not part of the quote"
    metric = item.get("metric")
    if metric not in METRICS:
        return None, f"unknown metric {metric!r}"

    currency = (item.get("currency") or currency_of(value_raw) or "").upper() or None
    if metric in COUNT_METRICS:
        unit, currency = "count", None
    elif metric in ("revenue", "funding_raised"):
        unit = currency or "unknown currency"
    else:
        unit = (item.get("unit") or "").strip().lower() or None

    return {
        "chunk_id": chunk["id"],
        "metric": metric,
        "metric_label": (item.get("metric_label") or "").strip() or None if metric == "other" else None,
        "value": normalize_number(value_raw),
        "value_raw": value_raw,
        "unit": unit,
        "currency": currency,
        "as_of_date": _checked_date(item.get("as_of_date"), chunk["text"]),
        "quote": quote,
    }, ""


def _checked_date(value: str | None, chunk_text: str) -> str | None:
    """Keep the as-of date only if it is a valid ISO date that is written in the chunk itself."""
    if not value:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    written = set(ISO_DATE_RE.findall(chunk_text)) | set(iso_dates(chunk_text.lower()))
    return value if value in written else None
