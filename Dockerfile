# Public read-only demo of Deal Room Assistant (DEMO_MODE=true). Keys come only from environment variables set
# on the host (Railway Variables); nothing secret is copied into the image (.env is in .dockerignore).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# CPU-only PyTorch first: the default Linux wheel brings several GB of CUDA libraries the demo never uses.
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

# The embedding model is downloaded at build time, so the server starts without network access to Hugging Face.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY app ./app
COPY static ./static
COPY config ./config
COPY skills ./skills
COPY data/demo_public.db ./data/demo_public.db

RUN useradd --create-home demo && chown -R demo /app/data /app/.cache
USER demo

# Demo defaults; Railway Variables override them. DB_PATH is the working copy the demo overwrites on every start
# and every day at 00:00 UTC; data/demo_public.db itself is only read.
ENV DEMO_MODE=true \
    DEMO_SOURCE_DB=data/demo_public.db \
    DB_PATH=data/demo_session.db \
    WEB_SEARCH_PROVIDER="" \
    HF_HUB_OFFLINE=1

# One process: the demo limits are counted in memory.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
