"""FastAPI app: JSON API under /api, single-page UI served from /static."""

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import PROJECT_ROOT, get_settings
from app.db import connect, init_db
from app.agent import answer_question
from app.extract import extract_metrics
from app.ingest import IngestError, ingest_document
from app.llm import LLMAuthError, LLMClient, LLMError, get_llm
from app.logs import get_run_log
from app.rag import DEFAULT_TOP_K, EmbeddingError, search
from app.rules import company_checks, get_metrics

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


def get_extract_llm() -> LLMClient:
    return get_llm("extract")


def get_chat_llm() -> LLMClient:
    return get_llm("chat")


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
    return {"status": "ok", "llm_provider": settings.llm_provider,
            "llm_extract_model": settings.llm_extract_model, "llm_chat_model": settings.llm_chat_model}


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
def upload_document(
    company_id: int,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    doc_date: str | None = Form(None),
    conn=Depends(get_db),
    llm: LLMClient = Depends(get_extract_llm),
):
    """Store the file (chunks + embeddings), then extract metrics.

    If extraction fails (e.g. no API key), the document stays stored and the response
    says so; extraction can be retried with POST /api/documents/{id}/extract.
    """
    require_company(conn, company_id)
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 20 MB.")
    try:
        result = ingest_document(conn, company_id, file.filename or "", doc_type.strip().lower(),
                                 (doc_date or "").strip() or None, data)
    except IngestError as e:
        raise HTTPException(400, str(e)) from None
    except EmbeddingError as e:
        raise HTTPException(503, str(e)) from None
    result["extraction"] = run_extraction(conn, result["document_id"], llm)
    return result


def run_extraction(conn: sqlite3.Connection, document_id: int, llm: LLMClient) -> dict:
    try:
        return extract_metrics(conn, document_id, llm)
    except LLMError as e:
        return {"status": "failed", "error": str(e)}


@app.post("/api/documents/{document_id}/extract")
def reextract_document(document_id: int, conn=Depends(get_db), llm: LLMClient = Depends(get_extract_llm)):
    if not conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone():
        raise HTTPException(404, f"Document {document_id} not found.")
    return run_extraction(conn, document_id, llm)


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


# --- retrieval (debug) ----------------------------------------------------------

@app.get("/api/companies/{company_id}/search")
def search_company(company_id: int, q: str, k: int = DEFAULT_TOP_K, conn=Depends(get_db)):
    """Hybrid search inside one company's materials, with both rankings shown for debugging."""
    require_company(conn, company_id)
    if not q.strip():
        raise HTTPException(400, "Query 'q' must not be empty.")
    try:
        return {"query": q, "results": search(conn, company_id, q, top_k=max(1, min(k, 50)))}
    except EmbeddingError as e:
        raise HTTPException(503, str(e)) from None


# --- metrics and program checks -------------------------------------------------

@app.get("/api/companies/{company_id}/metrics")
def list_metrics(company_id: int, conn=Depends(get_db)):
    require_company(conn, company_id)
    return get_metrics(conn, company_id)


@app.get("/api/companies/{company_id}/checks")
def get_checks(company_id: int, conn=Depends(get_db)):
    """Conflicts, unclear-basis metrics and missing key metrics, all decided by program rules."""
    require_company(conn, company_id)
    return company_checks(conn, company_id)


# --- Q&A agent ----------------------------------------------------------------------

class QuestionIn(BaseModel):
    question: str


def llm_http_error(e: LLMError) -> HTTPException:
    if isinstance(e, LLMAuthError):
        return HTTPException(503, f"API key missing. {e}")
    return HTTPException(502, f"Model call failed: {e}")


@app.post("/api/companies/{company_id}/ask")
def ask(company_id: int, body: QuestionIn, conn=Depends(get_db), llm: LLMClient = Depends(get_chat_llm)):
    require_company(conn, company_id)
    if not body.question.strip():
        raise HTTPException(400, "Question is required.")
    try:
        return answer_question(conn, company_id, body.question.strip(), llm)
    except LLMError as e:
        raise llm_http_error(e) from None
    except EmbeddingError as e:
        raise HTTPException(503, str(e)) from None


@app.get("/api/runs/{run_id}/log")
def run_log(run_id: str, conn=Depends(get_db)):
    """Every logged step of one agent run: question, tool calls with inputs/outputs, drops, final."""
    log = get_run_log(conn, run_id)
    if not log:
        raise HTTPException(404, f"Run {run_id} not found.")
    return log
