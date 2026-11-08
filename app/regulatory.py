"""Regulatory checklist: does a company's material mention what its industry usually needs?

The checklists live in config/regulatory.toml, not in code. The program picks the industry from the
company's files (the configured words with the most hits), then searches every file of the company for
each item's terms. An item with no hit is "Not addressed in materials": the files do not mention it.
That is never stated as a breach of a rule.
"""

import re
import sqlite3
import tomllib
from functools import lru_cache
from pathlib import Path

from app.citations import normalize_ws
from app.config import PROJECT_ROOT

CHECKLIST_PATH = PROJECT_ROOT / "config" / "regulatory.toml"
ADDRESSED, NOT_ADDRESSED = "Addressed in materials", "Not addressed in materials"


@lru_cache
def load_checklists(path: Path = CHECKLIST_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _pattern(terms: list[str]) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9])", re.IGNORECASE)


def _chunks(conn: sqlite3.Connection, company_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT ch.text, ch.loc_label, d.filename FROM chunks ch JOIN documents d ON d.id = ch.document_id"
                        " WHERE ch.company_id = ? ORDER BY d.doc_date, d.id, ch.chunk_index", (company_id,)).fetchall()


def detect_industry(chunks: list[sqlite3.Row], config: dict) -> str | None:
    text = " ".join(ch["text"] for ch in chunks)
    hits = {key: len(_pattern(ind["match"]).findall(text)) for key, ind in config["industries"].items()}
    best = max(hits, key=hits.get, default=None)
    return best if best and hits[best] >= config.get("min_hits", 3) else None


def check(conn: sqlite3.Connection, company_id: int, config: dict | None = None) -> dict:
    config = config or load_checklists()
    chunks = _chunks(conn, company_id)
    industry = detect_industry(chunks, config)
    if industry is None:
        return {"industry": None, "items": []}
    spec = config["industries"][industry]
    items = []
    for item in spec["items"]:
        pattern = _pattern(item["terms"])
        hit = next((ch for ch in chunks if pattern.search(ch["text"])), None)
        found = {"status": ADDRESSED, "quote": _sentence(hit["text"], pattern), "filename": hit["filename"],
                 "loc_label": hit["loc_label"], "question": None} if hit else \
            {"status": NOT_ADDRESSED, "quote": None, "filename": None, "loc_label": None,
             "question": {"text": item["question"], "sources": []}}
        items.append({"id": item["id"], "name": item["name"], "why": item["why"]} | found)
    return {"industry": {"id": industry, "name": spec["name"]}, "items": items}


def _sentence(text: str, pattern: re.Pattern) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", normalize_ws(text))
    return next((s for s in sentences if pattern.search(s)), normalize_ws(text))
