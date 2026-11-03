"""FastAPI app: JSON API under /api, single-page UI served from /static."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, get_settings
from app.db import connect, init_db

STATIC_DIR = PROJECT_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.db = connect(settings.db_path)
    init_db(app.state.db)
    yield
    app.state.db.close()


app = FastAPI(title="Deal Room Assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    settings = get_settings()
    return {"status": "ok", "llm_provider": settings.llm_provider, "llm_model": settings.llm_model}
