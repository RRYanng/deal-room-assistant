"""Retrieval: tokenizer, RRF math, company isolation, quality on sample data, replacement.

Quality and isolation tests use the real embedding model (downloaded once, then cached).
"""

import json
from pathlib import Path

import numpy as np
import pytest

from app.db import connect, init_db
from app.ingest import ingest_document
from app.rag import RRF_K, Embedder, EmbeddingError, get_embedder, search, tokenize

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(scope="module")
def sample_db():
    """In-memory DB with every initially-uploaded sample file. Returns (conn, {company: id})."""
    conn = connect(":memory:")
    init_db(conn)
    ids = {}
    for m in json.loads((SAMPLES / "manifest.json").read_text()):
        if not m["initial"]:
            continue
        if m["company"] not in ids:
            ids[m["company"]] = conn.execute("INSERT INTO companies(name) VALUES (?)", (m["company"],)).lastrowid
        path = SAMPLES / m["path"]
        ingest_document(conn, ids[m["company"]], path.name, m["doc_type"], m["doc_date"], path.read_bytes())
    yield conn, ids
    conn.close()


def company_of_chunk(conn, chunk_id):
    return conn.execute("SELECT company_id FROM chunks WHERE id = ?", (chunk_id,)).fetchone()[0]


# --- tokenizer --------------------------------------------------------------------

def test_tokenizer_keeps_numbers_and_dates():
    tokens = tokenize("As of 2026-06-30 we had 100 customers and 1,250 sign-ups; raised $1.2M.")
    assert {"2026-06-30", "100", "1250", "1.2"} <= set(tokens)
    assert "customer" in tokens  # plural folded
    assert "we" not in tokens and "had" not in tokens  # stopwords dropped


def test_tokenizer_adds_iso_token_for_written_dates():
    assert "2026-06-30" in tokenize("As of June 30, 2026, 100 businesses registered.")
    assert "2026-03-31" in tokenize("as of 31 March 2026")


# --- RRF math (deterministic stub embedder) ------------------------------------------

class KeywordEmbedder:
    """Vector = counts of a few fixed keywords. Lets the test know the vector ranking exactly."""
    model_name = "keyword-stub"
    VOCAB = ["apple", "banana", "cherry"]

    def embed(self, texts):
        vecs = np.array([[t.lower().count(w) for w in self.VOCAB] for t in texts], dtype=np.float32) + 1e-3
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def test_rrf_score_is_sum_of_reciprocal_ranks():
    conn = connect(":memory:")
    init_db(conn)
    cid = conn.execute("INSERT INTO companies(name) VALUES ('X')").lastrowid
    text = b"apple apple banana.\n\ncherry pie recipe.\n\nbanana bread with apple."
    ingest_document(conn, cid, "fruit.txt", "notes", None, text, embedder=KeywordEmbedder())

    results = search(conn, cid, "apple", top_k=3, embedder=KeywordEmbedder())
    for r in results:
        expected = 1 / (RRF_K + r["vector_rank"])
        if r["bm25_rank"] is not None:
            expected += 1 / (RRF_K + r["bm25_rank"])
        assert r["score"] == pytest.approx(expected)
    assert results[0]["text"] == "apple apple banana."
    cherry = next(r for r in results if r["text"].startswith("cherry"))
    assert cherry["bm25_rank"] is None  # no shared term -> no BM25 contribution
    assert set(results[0]) >= {"chunk_id", "text", "loc_label", "bm25_rank", "vector_rank", "score"}


# --- company isolation --------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "paying customers", "registered customers", "Kitebridge credit lines", "410 paying customers",
    "1,250 businesses", "total funding raised", "Lee Park default report", "next steps",
])
def test_search_never_returns_other_company_chunks(sample_db, query):
    conn, ids = sample_db
    a, b = ids["Tidewell Insurance"], ids["Kitebridge Finance"]
    for company, other_prefix in ((a, "kitebridge"), (b, "tidewell")):
        results = search(conn, company, query, top_k=50)
        assert results
        assert all(company_of_chunk(conn, r["chunk_id"]) == company for r in results)
        assert not any(r["filename"].startswith(other_prefix) for r in results)


# --- retrieval quality on sample data ---------------------------------------------

def top3_labels(conn, company_id, query):
    return [r["loc_label"] for r in search(conn, company_id, query, top_k=3)]


def test_paying_customers_finds_meeting_notes(sample_db):
    conn, ids = sample_db
    assert "tidewell_meeting_notes.md ¶3" in top3_labels(conn, ids["Tidewell Insurance"], "paying customers")


def test_registered_customers_finds_deck(sample_db):
    conn, ids = sample_db
    assert "tidewell_deck.pdf p.3 ¶2" in top3_labels(conn, ids["Tidewell Insurance"], "registered customers")


def test_last_call_next_steps_finds_crm_rows(sample_db):
    conn, ids = sample_db
    labels = top3_labels(conn, ids["Tidewell Insurance"], "last call next steps")
    assert any(label.startswith("tidewell_crm.csv row") for label in labels)


def test_iso_date_query_matches_written_date(sample_db):
    conn, ids = sample_db
    results = search(conn, ids["Tidewell Insurance"], "2026-06-30", top_k=6)
    bm25_hits = {r["loc_label"] for r in results if r["bm25_rank"]}
    assert {"tidewell_deck.pdf p.3 ¶2", "tidewell_meeting_notes.md ¶3"} <= bm25_hits


# --- replacement and stored vectors --------------------------------------------------

def test_replaced_file_chunks_never_returned():
    conn = connect(":memory:")
    init_db(conn)
    cid = conn.execute("INSERT INTO companies(name) VALUES ('X')").lastrowid
    old = ingest_document(conn, cid, "notes.md", "notes", None, b"We had 60 paying customers.")
    old_ids = {r["chunk_id"] for r in search(conn, cid, "paying customers")}
    assert old_ids

    ingest_document(conn, cid, "notes.md", "notes", None, b"We had 75 paying customers in total.")
    results = search(conn, cid, "paying customers", top_k=50)
    assert results and not ({r["chunk_id"] for r in results} & old_ids)
    assert all("60" not in r["text"] for r in results)
    assert all(r["document_id"] != old["document_id"] for r in results)


def test_upload_stores_normalized_vectors(sample_db):
    conn, _ = sample_db
    row = conn.execute("SELECT embedding, embedding_model FROM chunks LIMIT 1").fetchone()
    vec = np.frombuffer(row["embedding"], dtype=np.float32)
    assert vec.shape == (384,)
    assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-4)
    assert row["embedding_model"] == get_embedder().model_name


def test_missing_vectors_are_backfilled_on_search():
    conn = connect(":memory:")
    init_db(conn)
    cid = conn.execute("INSERT INTO companies(name) VALUES ('X')").lastrowid
    did = conn.execute("INSERT INTO documents(company_id, filename, doc_type) VALUES (?, 'a.md', 'notes')",
                       (cid,)).lastrowid
    conn.execute("INSERT INTO chunks(document_id, company_id, chunk_index, text, loc_label)"
                 " VALUES (?, ?, 0, 'Sixty paying customers.', 'a.md ¶1')", (did, cid))
    assert search(conn, cid, "paying customers")[0]["loc_label"] == "a.md ¶1"
    assert conn.execute("SELECT embedding IS NOT NULL FROM chunks").fetchone()[0] == 1


def test_bad_embedding_model_gives_clear_error():
    with pytest.raises(EmbeddingError, match="Could not load embedding model 'no/such-model-xyz-123'"):
        Embedder("no/such-model-xyz-123")
