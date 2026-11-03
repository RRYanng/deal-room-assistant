"""Retrieval: embeddings, BM25 and hybrid search with Reciprocal Rank Fusion (RRF).

search() always filters by company first, then ranks only that company's chunks:
  1. BM25 over the company's chunks (built at query time; the data set is small)
  2. cosine similarity between the query vector and stored chunk vectors
  3. RRF: score = sum over both rankings of 1 / (RRF_K + rank)
"""

import re
import sqlite3
from functools import lru_cache

import numpy as np
from rank_bm25 import BM25Okapi

from app.config import get_settings

RRF_K = 60
DEFAULT_TOP_K = 6

STOPWORDS = frozenset(
    "a an and are as at be been but by did do does for from had has have how i in is it its "
    "many much of on or our so than that the their them there these they this to was we were "
    "what when where which who will with you your".split()
)
# Dates first, then numbers (keeping decimals and thousands separators), then words.
TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d+(?:[.,]\d+)*|[a-z]+")
MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
# "June 30, 2026" or "30 June 2026" -> extra token "2026-06-30", so ISO-date queries match.
WORD_DATE_RE = re.compile(
    r"\b(?:(?P<m1>[a-z]+)\.? (?P<d1>\d{1,2}),? (?P<y1>\d{4})|(?P<d2>\d{1,2}) (?P<m2>[a-z]+)\.?,? (?P<y2>\d{4}))\b")


class EmbeddingError(RuntimeError):
    """The embedding model could not be loaded or used."""


# --- embeddings -----------------------------------------------------------------

class Embedder:
    """Wraps a sentence-transformers model. The model is downloaded once and then read
    from the Hugging Face cache (~/.cache/huggingface) on later runs."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
        except Exception as e:
            raise EmbeddingError(
                f"Could not load embedding model '{model_name}' ({type(e).__name__}: {e}). "
                "Check EMBEDDING_MODEL, and network access for the first download."
            ) from e

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return one L2-normalized float32 vector per text (so dot product = cosine)."""
        vectors = self._model.encode(texts, batch_size=32, normalize_embeddings=True,
                                     convert_to_numpy=True, show_progress_bar=False)
        return vectors.astype(np.float32)


@lru_cache(maxsize=2)
def _load_embedder(model_name: str) -> Embedder:
    return Embedder(model_name)


def get_embedder() -> Embedder:
    return _load_embedder(get_settings().embedding_model)


def to_blob(vector: np.ndarray) -> bytes:
    return vector.astype(np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def backfill_embeddings(conn: sqlite3.Connection, company_id: int, embedder: Embedder) -> int:
    """Embed chunks that have no vector, or a vector from a different model. Returns the count."""
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE company_id = ?"
        " AND (embedding IS NULL OR embedding_model IS NOT ?)",
        (company_id, embedder.model_name),
    ).fetchall()
    if rows:
        vectors = embedder.embed([r["text"] for r in rows])
        with conn:
            conn.executemany(
                "UPDATE chunks SET embedding = ?, embedding_model = ? WHERE id = ?",
                [(to_blob(v), embedder.model_name, r["id"]) for r, v in zip(rows, vectors)],
            )
    return len(rows)


# --- BM25 -----------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Lowercase; keep dates and numbers as whole tokens; drop stopwords; light plural folding."""
    text = text.lower()
    tokens = iso_dates(text)
    for tok in TOKEN_RE.findall(text):
        if tok[0].isdigit():
            tokens.append(tok.replace(",", ""))  # "1,250" -> "1250"
        elif tok not in STOPWORDS:
            tokens.append(_singular(tok))
    return tokens


def iso_dates(text: str) -> list[str]:
    found = []
    for m in WORD_DATE_RE.finditer(text):
        month = m["m1"] or m["m2"]
        month_no = MONTHS.get(month) or next((n for name, n in MONTHS.items()
                                              if len(month) >= 3 and name.startswith(month)), None)
        day, year = int(m["d1"] or m["d2"]), m["y1"] or m["y2"]
        if month_no and 1 <= day <= 31:
            found.append(f"{year}-{month_no:02d}-{day:02d}")
    return found


def _singular(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("sses"):
        return word[:-2]  # businesses -> business
    if word.endswith("ies"):
        return word[:-3] + "y"  # companies -> company
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]  # customers -> customer
    return word


# --- hybrid search --------------------------------------------------------------

def search(conn: sqlite3.Connection, company_id: int, query: str, top_k: int = DEFAULT_TOP_K,
           embedder: Embedder | None = None) -> list[dict]:
    embedder = embedder or get_embedder()
    backfill_embeddings(conn, company_id, embedder)

    rows = conn.execute(
        "SELECT ch.id, ch.document_id, ch.text, ch.loc_label, ch.embedding, d.filename, d.doc_type"
        " FROM chunks ch JOIN documents d ON d.id = ch.document_id"
        " WHERE ch.company_id = ? ORDER BY ch.id",
        (company_id,),
    ).fetchall()
    if not rows or not query.strip():
        return []

    # BM25: rank only chunks that share at least one term with the query.
    bm25 = BM25Okapi([tokenize(r["text"]) or ["<empty>"] for r in rows])
    bm25_scores = bm25.get_scores(tokenize(query))
    bm25_order = [i for i in np.argsort(-bm25_scores, kind="stable") if bm25_scores[i] > 0]
    bm25_rank = {i: rank for rank, i in enumerate(bm25_order, start=1)}

    # Vector: every chunk gets a rank by cosine similarity.
    matrix = np.vstack([from_blob(r["embedding"]) for r in rows])
    vec_scores = matrix @ embedder.embed([query])[0]
    vec_order = np.argsort(-vec_scores, kind="stable")
    vec_rank = {int(i): rank for rank, i in enumerate(vec_order, start=1)}

    results = []
    for i, r in enumerate(rows):
        score = 1 / (RRF_K + vec_rank[i])
        if i in bm25_rank:
            score += 1 / (RRF_K + bm25_rank[i])
        results.append({
            "chunk_id": r["id"],
            "document_id": r["document_id"],
            "filename": r["filename"],
            "doc_type": r["doc_type"],
            "loc_label": r["loc_label"],
            "text": r["text"],
            "bm25_rank": bm25_rank.get(i),
            "bm25_score": round(float(bm25_scores[i]), 4),
            "vector_rank": vec_rank[i],
            "vector_score": round(float(vec_scores[i]), 4),
            "score": score,
        })
    results.sort(key=lambda x: (-x["score"], x["chunk_id"]))
    return results[:top_k]
