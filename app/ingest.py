"""Import: parse an uploaded file into located chunks and store it.

Paragraph rule (PDF, TXT, MD): blocks are separated by blank lines. A heading block
(a Markdown "#" line, or one short line without closing punctuation) is attached to
the block after it, so a chunk keeps its section title. Very long paragraphs are split
at sentence boundaries into parts that share the same paragraph number.

CSV (CRM) rule: one data row = one chunk, plus one row in crm_records.
"""

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pypdf import PdfReader

DOC_TYPES = ("deck", "notes", "crm")
CRM_COLUMNS = ("date", "contact", "summary", "next_steps")
TEXT_EXTENSIONS = (".pdf", ".txt", ".md")
MAX_CHUNK_CHARS = 1200
HEADING_MAX_CHARS = 60


class IngestError(ValueError):
    """The upload cannot be processed; the message is shown to the user as-is."""


@dataclass
class Chunk:
    text: str
    loc_label: str
    page: int | None = None
    para: int | None = None
    row_num: int | None = None


@dataclass
class ParsedDocument:
    chunks: list[Chunk]
    crm_rows: list[dict] = field(default_factory=list)  # aligned 1:1 with chunks for CSV


# --- validation ---------------------------------------------------------------

def validate_upload(filename: str, doc_type: str, doc_date: str | None) -> str:
    """Check metadata before parsing. Returns the cleaned filename."""
    name = Path(filename or "").name
    if not name:
        raise IngestError("File name is missing.")
    if doc_type not in DOC_TYPES:
        raise IngestError(f"Unknown material type '{doc_type}'. Use one of: {', '.join(DOC_TYPES)}.")
    ext = Path(name).suffix.lower()
    if doc_type == "crm" and ext != ".csv":
        raise IngestError("CRM records must be a .csv file.")
    if doc_type != "crm" and ext not in TEXT_EXTENSIONS:
        raise IngestError(
            f"Unsupported file format '{ext or name}' for {doc_type}. "
            f"Supported: .pdf (text-based), .txt, .md; CRM records: .csv."
        )
    if doc_date:
        _check_iso_date(doc_date, "Material date")
    return name


def _check_iso_date(value: str, what: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise IngestError(f"{what} '{value}' must be in YYYY-MM-DD format.") from None


# --- parsing --------------------------------------------------------------------

def parse_file(filename: str, data: bytes) -> ParsedDocument:
    if not data:
        raise IngestError(f"{filename} is empty.")
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return parse_pdf(filename, data)
    if ext in (".txt", ".md"):
        return parse_text(filename, _decode(filename, data))
    if ext == ".csv":
        return parse_crm_csv(filename, _decode(filename, data))
    raise IngestError(f"Unsupported file format '{ext}'.")


def _decode(filename: str, data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IngestError(f"{filename} is not valid UTF-8 text.") from None


def parse_pdf(filename: str, data: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    except Exception as e:  # pypdf raises many error types for damaged files
        raise IngestError(f"Could not read {filename} as a PDF ({type(e).__name__}).") from None

    chunks = []
    for page_no, text in enumerate(pages, start=1):
        for para_no, part, n_parts, body in _paragraph_chunks(text):
            label = f"{filename} p.{page_no} ¶{para_no}" + (f" part {part}/{n_parts}" if n_parts > 1 else "")
            chunks.append(Chunk(text=body, loc_label=label, page=page_no, para=para_no))
    if not chunks:
        raise IngestError(f"{filename} has no extractable text. Scanned PDFs (OCR) are not supported.")
    return ParsedDocument(chunks)


def parse_text(filename: str, text: str) -> ParsedDocument:
    chunks = [
        Chunk(text=body, para=para_no,
              loc_label=f"{filename} ¶{para_no}" + (f" part {part}/{n_parts}" if n_parts > 1 else ""))
        for para_no, part, n_parts, body in _paragraph_chunks(text)
    ]
    if not chunks:
        raise IngestError(f"{filename} contains no text.")
    return ParsedDocument(chunks)


def parse_crm_csv(filename: str, text: str) -> ParsedDocument:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in CRM_COLUMNS if c not in header]
    if missing:
        raise IngestError(
            f"{filename} is missing required CRM column(s): {', '.join(missing)}. "
            f"Expected columns: {', '.join(CRM_COLUMNS)}."
        )
    reader.fieldnames = header

    chunks, rows = [], []
    for row_num, raw in enumerate(reader, start=1):
        row = {k: (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        if row["date"]:
            _check_iso_date(row["date"], f"{filename} row {row_num}: date")
        text_line = "; ".join(f"{k}: {v}" for k, v in row.items())
        chunks.append(Chunk(text=text_line, loc_label=f"{filename} row {row_num}", row_num=row_num))
        rows.append({c: row[c] or None for c in CRM_COLUMNS} | {"row_num": row_num})
    if not chunks:
        raise IngestError(f"{filename} has a header but no data rows.")
    return ParsedDocument(chunks, rows)


def _paragraph_chunks(text: str):
    """Yield (para_no, part, n_parts, text) for each paragraph of a page or text file."""
    blocks = []
    for raw in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines:
            blocks.append(lines)

    paragraphs, pending_heading = [], []
    for lines in blocks:
        if _is_heading(lines):
            pending_heading.append(lines[0].lstrip("#").strip())
            continue
        if lines[0].startswith("#"):  # Markdown heading directly above its paragraph
            pending_heading.append(lines.pop(0).lstrip("#").strip())
        body = " ".join(lines)
        paragraphs.append("\n".join(pending_heading + [body]))
        pending_heading = []
    if pending_heading:
        paragraphs.append("\n".join(pending_heading))

    for para_no, para in enumerate(paragraphs, start=1):
        parts = _split_long(para)
        for i, part in enumerate(parts, start=1):
            yield para_no, i, len(parts), part


def _is_heading(lines: list[str]) -> bool:
    if len(lines) != 1:
        return False
    line = lines[0]
    return line.startswith("#") or (len(line) <= HEADING_MAX_CHARS and not line.endswith((".", "!", "?", ":")))


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current and len(current) + 1 + len(sentence) > MAX_CHUNK_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        parts.append(current)
    return parts


# --- storing --------------------------------------------------------------------

def ingest_document(conn: sqlite3.Connection, company_id: int, filename: str, doc_type: str,
                    doc_date: str | None, data: bytes) -> dict:
    """Validate, parse and store one file. Same filename for the same company replaces the old one.

    Parsing happens before any write, so a bad replacement file leaves the old version intact.
    """
    filename = validate_upload(filename, doc_type, doc_date)
    parsed = parse_file(filename, data)

    with conn:  # one transaction: either everything is stored or nothing changes
        old = conn.execute("SELECT id FROM documents WHERE company_id = ? AND filename = ?",
                           (company_id, filename)).fetchone()
        if old:
            conn.execute("DELETE FROM documents WHERE id = ?", (old["id"],))  # cascades
        doc_id = conn.execute(
            "INSERT INTO documents(company_id, filename, doc_type, doc_date) VALUES (?, ?, ?, ?)",
            (company_id, filename, doc_type, doc_date or None),
        ).lastrowid
        for index, chunk in enumerate(parsed.chunks):
            chunk_id = conn.execute(
                "INSERT INTO chunks(document_id, company_id, chunk_index, text, page, para, row_num, loc_label)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, company_id, index, chunk.text, chunk.page, chunk.para, chunk.row_num, chunk.loc_label),
            ).lastrowid
            if parsed.crm_rows:
                r = parsed.crm_rows[index]
                conn.execute(
                    "INSERT INTO crm_records(company_id, document_id, chunk_id, row_num, date, contact,"
                    " summary, next_steps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (company_id, doc_id, chunk_id, r["row_num"], r["date"], r["contact"],
                     r["summary"], r["next_steps"]),
                )

    return {"document_id": doc_id, "filename": filename, "replaced": bool(old),
            "chunks": len(parsed.chunks)}
