"""FastAPI app: JSON API under /api, single-page UI served from /static."""

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import PROJECT_ROOT, get_settings
from app.db import connect, init_db
from app.ingest import IngestError, ingest_document

STATIC_DIR = PROJECT_ROOT / "static"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_path = get_settings().db_path
    conn = connect(app.state.db_path)
    init_db(conn)
    conn.close()
    yield


app = FastAPI(title="Deal Room Assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_db(request: Request):
    """One SQLite connection per request (sqlite3 connections should not be shared across threads)."""
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def require_company(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Company {company_id} not found.")
    return row


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    settings = get_settings()
    return {"status": "ok", "llm_provider": settings.llm_provider, "llm_model": settings.llm_model}


# --- companies ----------------------------------------------------------------

class CompanyIn(BaseModel):
    name: str


@app.get("/api/companies")
def list_companies(conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT c.id, c.name, COUNT(d.id) AS documents FROM companies c"
        " LEFT JOIN documents d ON d.company_id = c.id GROUP BY c.id ORDER BY c.name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/companies", status_code=201)
def create_company(body: CompanyIn, conn=Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Company name is required.")
    try:
        with conn:
            company_id = conn.execute("INSERT INTO companies(name) VALUES (?)", (name,)).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Company '{name}' already exists.") from None
    return {"id": company_id, "name": name}


# --- documents ----------------------------------------------------------------

@app.get("/api/companies/{company_id}/documents")
def list_documents(company_id: int, conn=Depends(get_db)):
    require_company(conn, company_id)
    rows = conn.execute(
        "SELECT d.id, d.filename, d.doc_type, d.doc_date, d.uploaded_at, COUNT(ch.id) AS chunks"
        " FROM documents d LEFT JOIN chunks ch ON ch.document_id = d.id"
        " WHERE d.company_id = ? GROUP BY d.id ORDER BY d.uploaded_at, d.id",
        (company_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/companies/{company_id}/documents", status_code=201)
async def upload_document(
    company_id: int,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    doc_date: str | None = Form(None),
    conn=Depends(get_db),
):
    require_company(conn, company_id)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 20 MB.")
    try:
        return ingest_document(conn, company_id, file.filename or "", doc_type.strip().lower(),
                               (doc_date or "").strip() or None, data)
    except IngestError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/documents/{document_id}/chunks")
def list_chunks(document_id: int, conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT id, chunk_index, text, page, para, row_num, loc_label FROM chunks"
        " WHERE document_id = ? ORDER BY chunk_index",
        (document_id,),
    ).fetchall()
    if not rows and not conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone():
        raise HTTPException(404, f"Document {document_id} not found.")
    return [dict(r) for r in rows]
