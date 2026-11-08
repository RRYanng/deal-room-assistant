"""FastAPI app: JSON API under /api, single-page UI served from /static."""

import hashlib
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.config import PROJECT_ROOT, get_settings
from app.db import connect, init_db
from app.agent import OUT_OF_SCOPE_MESSAGE, answer_question, is_out_of_scope
from app.batch import BatchFile, get_batch, start_batch
from app.brief import brief_to_markdown, generate_brief
from app.claims import fail_interrupted_runs, get_run, latest_deck, latest_results, start_claim_check
from app.extract import extract_metrics
from app.ingest import IngestError, ingest_document
from app.intake import analyze_upload, consistency_warnings
from app.llm import LLMAuthError, LLMClient, LLMError, get_llm
from app.logs import get_run_log
from app.memo import build_memo, memo_to_markdown
from app.presentation import build_summary
from app import monitor
from app.rag import DEFAULT_TOP_K, EmbeddingError, search
from app.resolve import resolve_question_company
from app.reviews import ReviewError, apply_reviews, record_review, review_counts
from app.rules import company_checks, get_metrics, issue_counts
from app.web import WebSearchProvider, get_web_search

STATIC_DIR = PROJECT_ROOT / "static"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_path = get_settings().db_path
    conn = connect(app.state.db_path)
    init_db(conn)
    fail_interrupted_runs(conn)
    monitor.fail_interrupted_runs(conn)
    conn.close()
    yield


app = FastAPI(title="Deal Room Assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_ui_files(request: Request, call_next):
    """Browsers must check for a newer page and script, or an old page can run with a new script."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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


def asset_url(name: str, static_dir: Path = STATIC_DIR) -> str:
    """URL with a hash of the file's content, so a browser can never reuse an old copy of a changed file."""
    digest = hashlib.sha256((static_dir / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        html = html.replace(f"/static/{name}", asset_url(name))
    return HTMLResponse(html)


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


@app.post("/api/uploads/analyze")
def analyze(file: UploadFile = File(...), conn=Depends(get_db), llm: LLMClient = Depends(get_chat_llm)):
    """Suggest company, type and date for a file, for the user to confirm. Stores nothing."""
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 20 MB.")
    try:
        return analyze_upload(conn, file.filename or "", data, llm)
    except IngestError as e:
        raise HTTPException(400, str(e)) from None


class WarningsIn(BaseModel):
    chosen_company: str
    suggested_company: str | None = None
    mentioned: list[str] = []
    filename: str = ""


@app.post("/api/uploads/warnings")
def upload_warnings(body: WarningsIn, conn=Depends(get_db)):
    """Warnings for the company currently chosen in the confirmation dialog."""
    return consistency_warnings(conn, body.chosen_company, body.suggested_company, body.mentioned, body.filename)


class BatchItemIn(BaseModel):
    company_id: int | None = None
    new_company: str | None = None
    doc_type: str
    doc_date: str | None = None


def read_batch_file(file: UploadFile, item: BatchItemIn) -> BatchFile:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    return BatchFile(file.filename or "", data, item.doc_type.strip().lower(), (item.doc_date or "").strip() or None,
                     item.company_id, item.new_company,
                     error="File is larger than 20 MB." if len(data) > MAX_UPLOAD_BYTES else None)


@app.post("/api/uploads/batch", status_code=202)
def upload_batch(request: Request, files: list[UploadFile] = File(...), items: str = Form(...),
                 llm: LLMClient = Depends(get_extract_llm)):
    """Store several files and extract their metrics in the background; read progress with GET.

    `items` is a JSON list with one entry per file. A row with no company refuses the whole batch
    before anything is stored; any other problem fails only its own row.
    """
    try:
        rows = TypeAdapter(list[BatchItemIn]).validate_json(items)
    except ValidationError:
        rows = []
    if len(rows) != len(files):
        raise HTTPException(400, "The list of file details does not match the files.")
    no_company = [f.filename or "" for f, r in zip(files, rows)
                  if r.company_id is None and not (r.new_company or "").strip()]
    if no_company:
        raise HTTPException(400, f"Choose a company for: {', '.join(no_company)}.")
    return start_batch(request.app.state.db_path, [read_batch_file(f, r) for f, r in zip(files, rows)], llm)


@app.get("/api/uploads/batch/{batch_id}")
def upload_batch_status(batch_id: str):
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(404, f"Batch {batch_id} not found.")
    return batch


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


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int, conn=Depends(get_db)):
    """Remove a document; its chunks, metrics and CRM rows are deleted with it (cascade)."""
    with conn:
        deleted = conn.execute("DELETE FROM documents WHERE id = ?", (document_id,)).rowcount
    if not deleted:
        raise HTTPException(404, f"Document {document_id} not found.")
    return {"deleted": document_id}


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


@app.get("/api/issues")
def list_issues(conn=Depends(get_db)):
    """Every company with its number of open issues, computed live from the saved materials."""
    rows = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"]} | issue_counts(conn, r["id"]) for r in rows]


# --- Q&A agent ----------------------------------------------------------------------

class QuestionIn(BaseModel):
    question: str


def llm_http_error(e: LLMError) -> HTTPException:
    return HTTPException(503 if isinstance(e, LLMAuthError) else 502, f"Model call failed: {e}")


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


class ChatQuestionIn(BaseModel):
    question: str
    last_company_id: int | None = None


NEEDS_COMPANY_MESSAGES = {
    "unknown": "Which company do you mean?",
    "multiple": "I can only answer about one company at a time. Which one should I answer about?",
}


@app.post("/api/ask")
def ask_any_company(body: ChatQuestionIn, conn=Depends(get_db), llm: LLMClient = Depends(get_chat_llm)):
    """Work out the company from the question, then answer. The server picks the company_id."""
    resolved = resolve_question_company(conn, body.question, body.last_company_id)
    if resolved["status"] == "unknown":
        try:
            if is_out_of_scope(body.question, llm):
                return {"resolution": "out_of_scope", "message": OUT_OF_SCOPE_MESSAGE}
        except LLMError as e:  # a failed check says nothing about the question, so report the failure
            raise llm_http_error(e) from None
    if resolved["status"] != "resolved":
        return {"resolution": "needs_company", "reason": resolved["status"],
                "message": NEEDS_COMPANY_MESSAGES[resolved["status"]], "companies": resolved["companies"]}
    answer = ask(resolved["company"]["id"], QuestionIn(question=body.question), conn, llm)
    if answer["out_of_scope"]:
        return {"resolution": "out_of_scope", "message": OUT_OF_SCOPE_MESSAGE}
    return {"resolution": "answered", "company": resolved["company"], "company_source": resolved["source"],
            "answer": answer}


@app.get("/api/runs/{run_id}/log")
def run_log(run_id: str, conn=Depends(get_db)):
    """Every logged step of one agent run: question, tool calls with inputs/outputs, drops, final."""
    log = get_run_log(conn, run_id)
    if not log:
        raise HTTPException(404, f"Run {run_id} not found.")
    return log


# --- one-click brief ----------------------------------------------------------------

class BriefIn(BaseModel):
    requirements: str = ""


@app.post("/api/companies/{company_id}/brief")
def brief(company_id: int, body: BriefIn, conn=Depends(get_db), llm: LLMClient = Depends(get_chat_llm)):
    require_company(conn, company_id)
    try:
        result = generate_brief(conn, company_id, body.requirements, llm)
    except LLMError as e:
        raise llm_http_error(e) from None
    except EmbeddingError as e:
        raise HTTPException(503, str(e)) from None
    result["markdown"] = brief_to_markdown(result)
    return result


# --- v2: claim check, reviews and memo --------------------------------------------------------

def get_web() -> WebSearchProvider | None:
    return get_web_search()


class ClaimCheckIn(BaseModel):
    document_id: int | None = None  # default: the company's newest deck


@app.post("/api/companies/{company_id}/claim-check", status_code=202)
def start_check(company_id: int, request: Request, body: ClaimCheckIn | None = None, conn=Depends(get_db),
                llm: LLMClient = Depends(get_extract_llm), web: WebSearchProvider | None = Depends(get_web)):
    """Check a deck's claims in the background; read progress with GET .../claim-runs/{run_id}."""
    require_company(conn, company_id)
    if conn.execute("SELECT 1 FROM claim_runs WHERE company_id = ? AND status = 'running'", (company_id,)).fetchone():
        raise HTTPException(409, "A claim check for this company is already running.")
    document_id = body.document_id if body and body.document_id else None
    if document_id is None:
        deck = latest_deck(conn, company_id)
        if not deck:
            raise HTTPException(400, "This company has no pitch deck to check.")
        document_id = deck["id"]
    try:
        run_id = start_claim_check(request.app.state.db_path, company_id, document_id, llm, web)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return get_run(conn, company_id, run_id)


@app.get("/api/companies/{company_id}/claim-runs/{run_id}")
def claim_run(company_id: int, run_id: int, conn=Depends(get_db)):
    run = get_run(conn, company_id, run_id)
    if not run:
        raise HTTPException(404, f"Claim check {run_id} not found for this company.")
    return run


@app.get("/api/companies/{company_id}/claims")
def list_claims(company_id: int, conn=Depends(get_db)):
    """The latest finished claim check with evidence and review state, plus a run still in progress."""
    require_company(conn, company_id)
    result = latest_results(conn, company_id)
    apply_reviews(conn, company_id, result["claims"])
    apply_reviews(conn, company_id, result["changes"], "drift")
    counts = review_counts(result["claims"])
    counts["dismissed"] += sum(c["review_state"] == "dismissed" for c in result["changes"])
    return result | {"counts": counts, "summary": build_summary(result["claims"], result["changes"])}


class ReviewIn(BaseModel):
    fingerprint: str
    decision: str
    reason: str = ""
    item_type: str = "claim"


@app.post("/api/companies/{company_id}/reviews", status_code=201)
def review(company_id: int, body: ReviewIn, conn=Depends(get_db)):
    require_company(conn, company_id)
    try:
        return record_review(conn, company_id, body.fingerprint, body.decision, body.reason, body.item_type)
    except ReviewError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/companies/{company_id}/memo")
def memo(company_id: int, conn=Depends(get_db)):
    require_company(conn, company_id)
    result = build_memo(conn, company_id)
    return result | {"markdown": memo_to_markdown(result)}


# --- v2: portfolio monitoring -------------------------------------------------------------------

@app.post("/api/companies/{company_id}/monitor", status_code=202)
def start_monitoring(company_id: int, request: Request, conn=Depends(get_db),
                     llm: LLMClient = Depends(get_extract_llm)):
    """Read the company's updates and compute risk signals in the background."""
    require_company(conn, company_id)
    if conn.execute("SELECT 1 FROM monitor_runs WHERE company_id = ? AND status = 'running'", (company_id,)).fetchone():
        raise HTTPException(409, "Monitoring for this company is already running.")
    if not monitor.update_documents(conn, company_id):
        raise HTTPException(400, "This company has no updates (notes files) to read.")
    return monitor.get_run(conn, company_id, monitor.start_monitor(request.app.state.db_path, company_id, llm))


@app.get("/api/companies/{company_id}/monitor-runs/{run_id}")
def monitor_run(company_id: int, run_id: int, conn=Depends(get_db)):
    run = monitor.get_run(conn, company_id, run_id)
    if not run:
        raise HTTPException(404, f"Monitoring run {run_id} not found for this company.")
    return run


@app.get("/api/companies/{company_id}/monitor")
def monitor_results(company_id: int, conn=Depends(get_db)):
    """Figures per update period and the signals computed from them, from the latest finished run."""
    require_company(conn, company_id)
    result = monitor.latest_results(conn, company_id)
    apply_reviews(conn, company_id, result["signals"], "signal")
    return result
