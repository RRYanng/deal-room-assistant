"""Batch upload: store the files first, then extract metrics, one file at a time, in the background.

Storing first makes every file searchable within seconds; extraction is what takes the time.
A row goes waiting -> uploading -> stored -> analyzing -> done, or to failed with the reason.
A failure stays on its own row, and the other rows go on.

Progress is kept in memory, because it only matters while the page is watching. If the server
restarts, the files stored so far stay stored and can be re-extracted from the Library.
"""

import logging
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from app.citations import normalize_ws
from app.db import connect
from app.extract import extract_metrics
from app.ingest import IngestError, ingest_document, parse_file, validate_upload
from app.llm import LLMClient, LLMError
from app.rag import EmbeddingError

log = logging.getLogger(__name__)

# One worker, so batches run one after another and never compete for the database or the model.
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-upload")
_batches: dict[str, dict] = {}


@dataclass
class BatchFile:
    filename: str
    data: bytes
    doc_type: str
    doc_date: str | None = None
    company_id: int | None = None
    new_company: str | None = None
    error: str | None = None  # already known to fail, such as a file over the size limit


def start_batch(db_path: Path | str, files: list[BatchFile], llm: LLMClient) -> dict:
    """Queue the files and return at once. Progress is read with get_batch()."""
    rows = [{"filename": f.filename, "status": "failed" if f.error else "waiting", "error": f.error,
             "company": None, "document_id": None, "replaced": False, "chunks": 0, "extraction": None} for f in files]
    batch = {"batch_id": uuid.uuid4().hex, "finished": False, "rows": rows}
    _batches[batch["batch_id"]] = batch
    _worker.submit(_run, db_path, batch, files, llm)
    return get_batch(batch["batch_id"])


def get_batch(batch_id: str) -> dict | None:
    batch = _batches.get(batch_id)
    return batch and {**batch, "rows": [dict(r) for r in batch["rows"]]}


def _run(db_path, batch: dict, files: list[BatchFile], llm: LLMClient) -> None:
    conn = connect(db_path)
    try:
        for row, file in zip(batch["rows"], files):
            if row["status"] == "waiting":
                _store(conn, row, file)
            file.data = b""  # stored or refused, the bytes are no longer needed
        for row in batch["rows"]:
            if row["status"] == "stored":
                _analyze(conn, row, llm)
    finally:
        conn.close()
        batch["finished"] = True


def _company_for(conn: sqlite3.Connection, file: BatchFile) -> dict:
    if file.company_id is not None:
        found = conn.execute("SELECT id, name FROM companies WHERE id = ?", (file.company_id,)).fetchone()
        if not found:
            raise IngestError(f"Company {file.company_id} not found.")
        return dict(found)
    name = normalize_ws(file.new_company or "")
    found = conn.execute("SELECT id, name FROM companies WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if found:
        return dict(found)
    with conn:
        return {"id": conn.execute("INSERT INTO companies(name) VALUES (?)", (name,)).lastrowid, "name": name}


def _store(conn: sqlite3.Connection, row: dict, file: BatchFile) -> None:
    row["status"] = "uploading"
    try:
        # Check the file before the company, so an unreadable file does not leave an empty new company.
        parse_file(validate_upload(file.filename, file.doc_type, file.doc_date), file.data)
        company = _company_for(conn, file)
        stored = ingest_document(conn, company["id"], file.filename, file.doc_type, file.doc_date, file.data)
    except (IngestError, EmbeddingError) as e:
        row.update(status="failed", error=str(e))
    except Exception as e:
        log.exception("Could not store %s", file.filename)
        row.update(status="failed", error=f"Could not store this file ({type(e).__name__}: {e}).")
    else:
        row.update(status="stored", company=company, replaced=stored["replaced"], chunks=stored["chunks"],
                   document_id=stored["document_id"])


def _analyze(conn: sqlite3.Connection, row: dict, llm: LLMClient) -> None:
    row["status"] = "analyzing"
    try:
        row.update(status="done", extraction=extract_metrics(conn, row["document_id"], llm))
    except (ValueError, sqlite3.IntegrityError):  # the document is gone, or went during the model call
        row.update(status="failed", error="This file was replaced or deleted before its metrics were extracted.")
    except LLMError as e:
        row.update(status="failed", error=f"Stored, but metrics were not extracted: {e}")
    except Exception as e:
        log.exception("Could not extract metrics from %s", row["filename"])
        row.update(status="failed", error=f"Stored, but metrics were not extracted ({type(e).__name__}: {e}).")
