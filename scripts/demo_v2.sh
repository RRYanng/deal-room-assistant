#!/usr/bin/env bash
# Start the v2 demo with one command:  scripts/demo_v2.sh
#
# Serves a fresh copy of data/demo_v2.db (built by scripts/build_demo_v2.py) at http://localhost:8002,
# so confirmations and dismissals made during a demo never change the prepared database and every
# start begins with 0 review records. Options:
#   --keep   reuse the copy from the last start (keeps what was confirmed or dismissed)
#   PORT=8003 scripts/demo_v2.sh   use another port
# Stop with Ctrl+C.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8002}"
DEMO_DB="data/demo_v2.db"
SESSION_DB="data/demo_v2_session.db"

if [[ ! -f "$DEMO_DB" ]]; then
  echo "$DEMO_DB not found. Build it first: .venv/bin/python scripts/build_demo_v2.py --from <v1.1 backup>" >&2
  exit 1
fi
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Stop that server or run: PORT=8003 scripts/demo_v2.sh" >&2
  exit 1
fi
if [[ "${1:-}" != "--keep" || ! -f "$SESSION_DB" ]]; then
  cp "$DEMO_DB" "$SESSION_DB"
  rm -f "$SESSION_DB-wal" "$SESSION_DB-shm"
fi

echo "Demo: http://localhost:$PORT  (database: $SESSION_DB, a copy of $DEMO_DB)"
DB_PATH="$SESSION_DB" exec .venv/bin/uvicorn app.main:app --port "$PORT"
