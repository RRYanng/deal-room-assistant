"""SQLite connection and schema.

Deleting a document cascades to its chunks, metrics and CRM records, so re-uploading
a file with the same name is just "delete old document row, insert new one".
IDs use AUTOINCREMENT so a replaced document never hands its old IDs to new rows
(a stale citation can then never point at different text).
Conflicts are not stored: they are computed from the metrics table on demand.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    doc_type     TEXT NOT NULL CHECK (doc_type IN ('deck', 'notes', 'crm')),
    doc_date     TEXT,                       -- ISO date of the material, may be unknown
    uploaded_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_id, filename)
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,           -- order within the document
    text         TEXT NOT NULL,
    page         INTEGER,                    -- PDF page (1-based)
    para         INTEGER,                    -- paragraph number (1-based, per page for PDF)
    row_num      INTEGER,                    -- CSV data row (1-based, header excluded)
    loc_label    TEXT NOT NULL,              -- human-readable, e.g. "deck.pdf p.3 ¶2"
    embedding    BLOB,                       -- float32 vector, L2-normalized
    embedding_model TEXT                     -- model that produced the vector
);
CREATE INDEX IF NOT EXISTS idx_chunks_company ON chunks(company_id);

CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id     INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    metric       TEXT NOT NULL CHECK (metric IN
                   ('registered_customers', 'paying_customers', 'revenue', 'funding_raised', 'other')),
    metric_label TEXT,                       -- free-text name when metric = 'other'
    value        REAL,                       -- normalized number (e.g. 1200000.0)
    value_raw    TEXT NOT NULL,              -- as written in the source (e.g. "$1.2M")
    unit         TEXT,
    currency     TEXT,
    as_of_date   TEXT,                       -- NULL means "unclear basis"
    quote        TEXT NOT NULL,              -- verbatim source text, verified against the chunk
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_metrics_company ON metrics(company_id);

CREATE TABLE IF NOT EXISTS crm_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id     INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
    row_num      INTEGER NOT NULL,
    date         TEXT,
    contact      TEXT,
    summary      TEXT,
    next_steps   TEXT
);
CREATE INDEX IF NOT EXISTS idx_crm_company ON crm_records(company_id);

CREATE TABLE IF NOT EXISTS agent_logs (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL,
    step         INTEGER NOT NULL,
    kind         TEXT NOT NULL,              -- e.g. 'tool_call', 'citation_dropped', 'final'
    tool         TEXT,
    input_json   TEXT,
    output_json  TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_run ON agent_logs(run_id);

-- v2: claim checks, web research and reviews. New tables only; the v1 tables above are unchanged.
-- Every table carries company_id, and every query filters on it.

CREATE TABLE IF NOT EXISTS claim_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,  -- the deck checked
    status       TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    stage        TEXT,                       -- what the run is doing now, shown as progress
    error        TEXT,
    notes_json   TEXT,                       -- steps skipped or failed, e.g. no web search configured
    started_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_claim_runs_company ON claim_runs(company_id);

CREATE TABLE IF NOT EXISTS claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES claim_runs(id) ON DELETE CASCADE,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id     INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,              -- fixed vocabulary, e.g. 'unit.gross_margin_pct'
    statement    TEXT NOT NULL,              -- short restatement by the model, for display only
    quote        TEXT NOT NULL,              -- verbatim from the chunk, verified
    value_raw    TEXT,                       -- the number as written, part of the quote
    value_lo     REAL,                       -- range read by app.numbers (equal for a single value)
    value_hi     REAL,
    fingerprint  TEXT NOT NULL,              -- company + type + key + numbers; never model wording
    status       TEXT NOT NULL CHECK (status IN ('contradicted', 'possibly_overstated',
                   'externally_supported', 'consistent_with_company_data', 'unverified'))
);
CREATE INDEX IF NOT EXISTS idx_claims_run ON claims(run_id);

CREATE TABLE IF NOT EXISTS web_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    run_id       INTEGER NOT NULL REFERENCES claim_runs(id) ON DELETE CASCADE,
    query        TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT,
    content      TEXT NOT NULL,              -- the text the search returned; quotes are checked against it
    fetched_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_web_sources_run ON web_sources(run_id);

CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id      INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source        TEXT NOT NULL CHECK (source IN ('internal', 'external', 'calculation')),
    stance        TEXT NOT NULL CHECK (stance IN ('supports', 'contradicts', 'overstates')),
    chunk_id      INTEGER REFERENCES chunks(id) ON DELETE CASCADE,          -- internal evidence
    web_source_id INTEGER REFERENCES web_sources(id) ON DELETE CASCADE,     -- external evidence
    quote         TEXT,                      -- verbatim from the chunk or the web source
    value_raw     TEXT,
    note          TEXT NOT NULL              -- written by the program
);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);

CREATE TABLE IF NOT EXISTS competitors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES claim_runs(id) ON DELETE CASCADE,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT,
    web_source_id INTEGER NOT NULL REFERENCES web_sources(id) ON DELETE CASCADE,
    quote         TEXT NOT NULL              -- verbatim from the source, contains the name
);
CREATE INDEX IF NOT EXISTS idx_competitors_run ON competitors(run_id);

-- Investor decisions. Append-only: the latest row for a fingerprint wins, so a decision can be undone.
-- Keyed by fingerprint, not by claim id, so a decision carries over to later runs of the same claim.
CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    item_type    TEXT NOT NULL CHECK (item_type IN ('claim', 'signal', 'drift')),
    fingerprint  TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('confirmed', 'dismissed')),
    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    item_json    TEXT,                       -- what the investor saw when deciding
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reviews_company ON reviews(company_id, fingerprint);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with foreign keys on and dict-like rows."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
