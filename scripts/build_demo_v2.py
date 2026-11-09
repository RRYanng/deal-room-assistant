"""Build the v2 demo database: the v1.1 demo data plus ClearClaim AI, checked with the real model.

Usage:  python scripts/build_demo_v2.py --from ../backups/deal_room_v1.1.db [--out data/demo_v2.db]

Steps, the same work the app does when a user uploads the files and presses the buttons:
  1. copy the v1.1 backup to --out and add the v2 tables (init_db)
  2. upload the eight ClearClaim AI files (samples/manifest_v2.json) and extract their metrics
  3. run one claim check on the newest deck (real model, web research with the configured provider)
  4. run portfolio monitoring on the quarterly updates
The result has no confirm/dismiss records. Calls the real model and the web search provider, so it
costs money (about $1; the build on 2026-11-08 cost $0.93 and 8 search credits) and needs ANTHROPIC_API_KEY and TAVILY_API_KEY in .env. It never writes to
data/deal_room.db and refuses to overwrite an existing file unless --force is given.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import monitor  # noqa: E402
from app.claims import create_run, latest_deck, run_claim_check  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.extract import extract_metrics  # noqa: E402
from app.ingest import ingest_document  # noqa: E402
from app.llm import get_llm  # noqa: E402
from app.usage import CountingLLM  # noqa: E402
from app.web import get_web_search  # noqa: E402

COMPANY = "ClearClaim AI"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", required=True, type=Path, help="the v1.1 database backup")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "demo_v2.db")
    parser.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    args = parser.parse_args()

    out = args.out.resolve()
    if out == (ROOT / "data" / "deal_room.db").resolve():
        sys.exit("Refusing to write to data/deal_room.db (the v1.1 demo database).")
    if out.exists() and not args.force:
        sys.exit(f"{out.name} already exists. Use --force to rebuild it.")
    settings = get_settings()
    if settings.llm_provider != "anthropic":
        sys.exit("The demo database is built with the real model: set LLM_PROVIDER=anthropic.")
    web = get_web_search(settings)
    if web is None:
        sys.exit("Web research is off or has no API key: set TAVILY_API_KEY in .env.")

    shutil.copyfile(args.source, out)
    conn = connect(out)
    init_db(conn)
    if conn.execute("SELECT 1 FROM companies WHERE name = ?", (COMPANY,)).fetchone():
        sys.exit(f"{args.source} already contains {COMPANY}; start from the v1.1 backup.")

    llm = get_llm("extract", settings)
    print(f"1. Copied {args.source.name} to {out.name}")
    company_id, upload_usage = upload_files(conn, llm)
    claim_usage = check_claims(conn, company_id, llm, web)
    monitor_usage = run_monitoring(conn, company_id, llm)

    reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    total = sum(u["estimated_usd"] or 0 for u in (upload_usage, claim_usage, monitor_usage))
    print(f"\nConfirm/dismiss records: {reviews}. Estimated model cost of this build: ${total:.2f}")
    conn.close()
    return 0


def upload_files(conn: sqlite3.Connection, llm) -> tuple[int, dict]:
    company_id = conn.execute("INSERT INTO companies(name) VALUES (?)", (COMPANY,)).lastrowid
    conn.commit()
    counting = CountingLLM(llm)
    for m in json.loads((ROOT / "samples" / "manifest_v2.json").read_text()):
        if m["company"] != COMPANY:
            continue
        path = ROOT / "samples" / m["path"]
        doc = ingest_document(conn, company_id, path.name, m["doc_type"], m["doc_date"], path.read_bytes())
        found = extract_metrics(conn, doc["document_id"], counting)
        print(f"2. Uploaded {path.name}: {doc['chunks']} chunks, {found['kept']} metrics")
    usage = counting.summary()
    print(f"   Metric extraction: {usage['model_calls']} model calls, about ${usage['estimated_usd'] or 0:.2f}")
    return company_id, usage


def check_claims(conn: sqlite3.Connection, company_id: int, llm, web) -> dict:
    run_id = create_run(conn, company_id, latest_deck(conn, company_id)["id"])
    print(f"3. Claim check of the newest deck (run {run_id}) ...")
    run_claim_check(conn, run_id, llm, web)
    _require_done(conn, "claim_runs", run_id)
    usage = _last_usage(conn, f"claims-run{run_id}")
    statuses = conn.execute("SELECT status, COUNT(*) FROM claims WHERE run_id = ? GROUP BY status", (run_id,))
    print("   " + ", ".join(f"{n} {s}" for s, n in statuses))
    print(f"   {usage['model_calls']} model calls, {usage['search_credits']} search credits, about "
          f"${usage['estimated_usd'] or 0:.2f}, {usage['seconds']} s")
    return usage


def run_monitoring(conn: sqlite3.Connection, company_id: int, llm) -> dict:
    run_id = monitor.create_run(conn, company_id)
    print(f"4. Portfolio monitoring (run {run_id}) ...")
    monitor.run_monitor(conn, run_id, llm)
    _require_done(conn, "monitor_runs", run_id)
    usage = _last_usage(conn, f"monitor-run{run_id}")
    for s in conn.execute("SELECT level, title FROM signals WHERE run_id = ?", (run_id,)):
        print(f"   {s['level']}: {s['title']}")
    print(f"   {usage['model_calls']} model calls, about ${usage['estimated_usd'] or 0:.2f}, {usage['seconds']} s")
    return usage


def _require_done(conn: sqlite3.Connection, table: str, run_id: int) -> None:
    run = conn.execute(f"SELECT status, error FROM {table} WHERE id = ?", (run_id,)).fetchone()
    if run["status"] != "done":
        sys.exit(f"   Run {run_id} failed: {run['error']}")


def _last_usage(conn: sqlite3.Connection, log_id: str) -> dict:
    row = conn.execute("SELECT output_json FROM agent_logs WHERE run_id = ? AND kind = 'usage' ORDER BY id DESC"
                       " LIMIT 1", (log_id,)).fetchone()
    return json.loads(row["output_json"])


if __name__ == "__main__":
    raise SystemExit(main())
