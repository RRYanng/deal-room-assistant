"""Event log in the agent_logs table (tool calls, dropped citations, ...), mirrored to stdout."""

import json
import logging
import sqlite3
from typing import Any

logger = logging.getLogger("deal_room")


def log_event(conn: sqlite3.Connection, run_id: str, step: int, kind: str, tool: str | None = None,
              input: Any = None, output: Any = None) -> None:
    conn.execute(
        "INSERT INTO agent_logs(run_id, step, kind, tool, input_json, output_json) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, step, kind, tool, json.dumps(input, default=str), json.dumps(output, default=str)),
    )
    conn.commit()
    logger.info("[%s] step=%s %s %s", run_id, step, kind, tool or "")


def get_run_log(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT step, kind, tool, input_json, output_json, created_at FROM agent_logs"
        " WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [{"step": r["step"], "kind": r["kind"], "tool": r["tool"],
             "input": json.loads(r["input_json"]), "output": json.loads(r["output_json"]),
             "created_at": r["created_at"]} for r in rows]
