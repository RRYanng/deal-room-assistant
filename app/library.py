"""Library: a one-line summary of each file, and a keyword search over file contents. No model is called.

A summary is put together from what is already stored: the file's type and date, then every metric extracted
from it (key metrics first). A file with no metrics shows its first meaningful sentence instead. The page shows
one line and opens the rest on a click.

search_files() uses the BM25 keyword index of app.rag (the same tokens: lowercase, plurals folded, stopwords
dropped) and counts how often each file mentions the query as a phrase.
"""

import re
import sqlite3
from datetime import date

from rank_bm25 import BM25Okapi

from app.rag import tokenize

SENTENCE_MAX = 300  # a first sentence longer than this is cut
DOC_TYPE_NAMES = {"deck": "Pitch deck", "notes": "Notes", "crm": "CRM records"}
# Standard metrics come first, then "other" metrics whose label names one of these, in this order.
KEY_LABEL_WORDS = ("market", "employees", "revenue", "cash", "burn", "margin", "growth")
STANDARD = {
    "paying_customers": "{} paying customers",
    "registered_customers": "{} registered customers",
    "revenue": "{} revenue",
    "funding_raised": "{} raised",
}
SKIP_LINE = "SYNTHETIC SAMPLE"
MIN_WORDS = 4  # shorter sentences ("Intro call.", "Date: 2025-12-08.") say too little
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def document_summaries(conn: sqlite3.Connection, company_id: int) -> dict[int, str]:
    """{document_id: summary} for every file of the company."""
    docs = conn.execute("SELECT id, doc_type, doc_date FROM documents WHERE company_id = ?",
                        (company_id,)).fetchall()
    metrics = conn.execute(
        "SELECT m.document_id, m.metric, m.metric_label, m.value_raw, m.unit FROM metrics m"
        " JOIN chunks ch ON ch.id = m.chunk_id WHERE m.company_id = ? ORDER BY ch.chunk_index, m.id",
        (company_id,),
    ).fetchall()
    by_doc: dict[int, list[sqlite3.Row]] = {}
    for m in metrics:
        by_doc.setdefault(m["document_id"], []).append(m)

    summaries = {}
    for d in docs:
        head = ", ".join(filter(None, [DOC_TYPE_NAMES.get(d["doc_type"], d["doc_type"]), _date(d["doc_date"])]))
        found = by_doc.get(d["id"])
        if found:
            summaries[d["id"]] = f"{head}: " + ", ".join(_metric_phrases(found))
        else:
            sentence = _first_sentence(conn, d["id"], d["doc_type"])
            summaries[d["id"]] = _cut(f"{head}: {sentence}" if sentence else head)
    return summaries


def _date(iso: str | None) -> str | None:
    try:
        day = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return f"{day:%b} {day.day}, {day.year}"


def _metric_phrases(metrics: list[sqlite3.Row]) -> list[str]:
    def rank(m):
        if m["metric"] in STANDARD:
            return 0
        label = (m["metric_label"] or "").lower()
        return next((i for i, word in enumerate(KEY_LABEL_WORDS, 1) if word in label), len(KEY_LABEL_WORDS) + 1)

    phrases = []
    for m in sorted(metrics, key=rank):  # stable: document order within each rank
        if m["metric"] in STANDARD:
            phrase = STANDARD[m["metric"]].format(m["value_raw"])
        else:
            phrase = _other_phrase(m["value_raw"], m["metric_label"] or "other", m["unit"] or "")
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases


def _other_phrase(value: str, label: str, unit: str) -> str:
    """"14 full-time employees"; a bare number whose unit the label does not name keeps the unit:
    "average time from registration to first quote: 4 minutes"."""
    label = _lower_first(label)
    if unit.lower() not in ("", "count", "usd", "%") and not re.search(r"[A-Za-z%$]", value) \
            and unit.lower() not in label.lower():
        return f"{label}: {value} {unit}"
    return f"{value} {label}"


def _lower_first(label: str) -> str:
    """"Full-time employees" -> "full-time employees", but "US dental care spending" stays as it is."""
    return label[0].lower() + label[1:] if label[1:2].islower() else label


def _cut(text: str, limit: int = SENTENCE_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _first_sentence(conn: sqlite3.Connection, document_id: int, doc_type: str) -> str | None:
    """The first sentence of at least MIN_WORDS words, skipping the SYNTHETIC SAMPLE line.
    CRM rows ("date: …; contact: …; summary: …") are read from their summary field."""
    for (text,) in conn.execute("SELECT text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                                (document_id,)):
        if doc_type == "crm":
            fields = dict(part.split(": ", 1) for part in text.split("; ") if ": " in part)
            lines = [fields.get("summary", "")]
        else:
            lines = text.splitlines()
        for line in lines:
            if SKIP_LINE in line:
                continue
            for sentence in SENTENCE_RE.split(line.strip()):
                if len(re.findall(r"[A-Za-z]+", sentence)) >= MIN_WORDS:
                    return sentence.strip()
    return None


# --- content search -------------------------------------------------------------------------

def search_files(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Files that mention the query (as a phrase of index tokens), best BM25 match first, with the count and
    the places. Each company's chunks are ranked by their own BM25 index, as in app.rag.search."""
    terms = tokenize(query)
    if not terms:
        return []
    found = []
    for company in conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall():
        rows = conn.execute(
            "SELECT ch.text, ch.loc_label, d.id AS document_id, d.filename, d.doc_type, d.doc_date"
            " FROM chunks ch JOIN documents d ON d.id = ch.document_id"
            " WHERE ch.company_id = ? ORDER BY d.id, ch.chunk_index",
            (company["id"],),
        ).fetchall()
        if not rows:
            continue
        tokens = [tokenize(r["text"]) for r in rows]
        scores = BM25Okapi([t or ["<empty>"] for t in tokens]).get_scores(terms)
        files: dict[int, dict] = {}
        for r, toks, score in zip(rows, tokens, scores):
            mentions = _phrase_count(toks, terms)
            if not mentions:
                continue
            f = files.setdefault(r["document_id"], {
                "document_id": r["document_id"], "company_id": company["id"], "company": company["name"],
                "filename": r["filename"], "doc_type": r["doc_type"], "doc_date": r["doc_date"],
                "mentions": 0, "locations": [], "score": 0.0,
            })
            f["mentions"] += mentions
            f["locations"].append(_location(r["loc_label"], r["filename"]))
            f["score"] += float(score)
        found.extend(files.values())
    found.sort(key=lambda f: (-f["score"], -f["mentions"], f["document_id"]))
    for f in found:
        f["score"] = round(f["score"], 4)
    return found


def _phrase_count(tokens: list[str], terms: list[str]) -> int:
    n = len(terms)
    return sum(tokens[i:i + n] == terms for i in range(len(tokens) - n + 1))


def _location(loc_label: str, filename: str) -> str:
    return loc_label[len(filename):].strip() if loc_label.startswith(filename) else loc_label
