"""Portfolio monitoring: figures from each update, and risk signals computed by the program.

One run reads a company's notes files oldest first (instructions: skills/portfolio-monitor/SKILL.md):
  1. the model extracts period end, cash, monthly net burn, revenue, growth, people and milestones,
     each with a chunk_id and a quote; the program keeps an item only if its quote is verbatim in that
     chunk and its number or date is part of the quote, and reads every number itself
  2. a file is a quarterly update only if it reports cash, net burn or revenue (meeting notes usually
     do not, so they are not periods)
  3. the program computes three signals per period (signals_for):
     - cash runway: cash / monthly net burn < 12 months is a warning; with revenue growth from the
       previous update under 10% it is high
     - key person departure: a founder, C-level, VP or Head of from the previous update stated as
       departed, or missing from this update's team list
     - repeated milestone slip: the same milestone's target date moved later at least twice, or its
       date passed without it being done
The model never writes a signal. Further signals (competitors, customer concentration, regulation,
next round) are described in the README but not built.
"""

import hashlib
import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from app.citations import normalize_ws, quote_in_text
from app.db import connect
from app.llm import LLMClient, LLMError
from app.logs import log_event
from app.numbers import normalize_number
from app.rag import iso_dates
from app.skills import skill_prompt
from app.usage import CountingLLM

log = logging.getLogger(__name__)

RUNWAY_WARNING_MONTHS = 12
SLOW_GROWTH_PCT = 10
SLIPS_FOR_SIGNAL = 2
KPIS = ("cash", "monthly_net_burn", "revenue", "revenue_growth_pct")
PERIOD_KPIS = ("cash", "monthly_net_burn", "revenue")  # a file reporting none of these is not an update
KEY_ROLE = re.compile(r"\b(founder|co-founder|ceo|cto|cfo|coo|cpo|cro|chief|president|vp|vice president|head of)\b",
                      re.IGNORECASE)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_item = {"chunk_id": {"type": "integer"}, "quote": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "period_end": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "period_chunk_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "period_quote": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "metrics": {"type": "array", "items": {"type": "object", "properties": {
            "kpi": {"type": "string", "enum": list(KPIS)}, "value_raw": {"type": "string"}, **_item},
            "required": ["kpi", "value_raw", "chunk_id", "quote"], "additionalProperties": False}},
        "people": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string"},
            "status": {"type": "string", "enum": ["present", "departed"]}, **_item},
            "required": ["name", "role", "status", "chunk_id", "quote"], "additionalProperties": False}},
        "milestones": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "target_date": {"type": "string"},
            "status": {"type": "string", "enum": ["planned", "done", "delayed"]}, **_item},
            "required": ["name", "target_date", "status", "chunk_id", "quote"], "additionalProperties": False}},
    },
    "required": ["period_end", "period_chunk_id", "period_quote", "metrics", "people", "milestones"],
    "additionalProperties": False,
}


# --- 1. extraction ----------------------------------------------------------------------

def build_prompt(doc: sqlite3.Row, chunks: list[sqlite3.Row], people: list[str], milestones: list[str]) -> str:
    lines = [f"Company: {doc['company_name']}",
             f"Update: {doc['filename']} (material date: {doc['doc_date'] or 'unknown'})",
             "People named in earlier updates: " + (", ".join(people) if people else "none"),
             "Milestones named in earlier updates: " + (", ".join(milestones) if milestones else "none"),
             "", "Chunks:"]
    for ch in chunks:
        lines += [f"[chunk_id={ch['id']}] ({ch['loc_label']})", ch["text"], ""]
    return "\n".join(lines)


def extract_period(conn: sqlite3.Connection, doc: sqlite3.Row, llm: LLMClient, people: list[str],
                   milestones: list[str], log_id: str) -> dict:
    """Checked figures, people and milestones of one file. Raises LLMError on model failure."""
    chunks = conn.execute("SELECT id, text, loc_label FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                          (doc["id"],)).fetchall()
    by_id = {ch["id"]: ch for ch in chunks}
    answer = llm.complete_json(skill_prompt("portfolio-monitor", "Extract KPIs"),
                               build_prompt(doc, chunks, people, milestones), SCHEMA)
    out = {"metrics": {}, "people": [], "milestones": [], "period_end": None}

    def checked(item, *parts) -> dict | None:
        """The item's location and quote if the quote is in its chunk and every part is in the quote."""
        chunk = by_id.get(item.get("chunk_id")) if isinstance(item, dict) else None
        quote = normalize_ws(str((item or {}).get("quote", "")))
        if chunk is None or not quote_in_text(quote, chunk["text"]):
            log_event(conn, log_id, 1, "monitor_item_dropped", input=item, output="quote is not verbatim in the chunk")
            return None
        missing = [p for p in parts if p and p.lower() not in quote.lower()]
        if missing:
            log_event(conn, log_id, 1, "monitor_item_dropped", input=item, output=f"not in the quote: {missing}")
            return None
        return {"chunk_id": chunk["id"], "loc_label": chunk["loc_label"], "filename": doc["filename"], "quote": quote}

    for m in answer.get("metrics") or []:
        value_raw = normalize_ws(str(m.get("value_raw", "")))
        source = checked(m, value_raw)
        value = normalize_number(value_raw.replace("$", "")) if source else None
        if source and value is not None and m.get("kpi") in KPIS and m["kpi"] not in out["metrics"]:
            out["metrics"][m["kpi"]] = {"value": value, "value_raw": value_raw, **source}
    for p in answer.get("people") or []:
        name = normalize_ws(str(p.get("name", "")))
        source = checked(p, name)
        if source and name:
            out["people"].append({"name": name, "role": normalize_ws(str(p.get("role", ""))),
                                  "status": p.get("status") if p.get("status") == "departed" else "present", **source})
    for ms in answer.get("milestones") or []:
        target = str(ms.get("target_date", ""))
        source = checked(ms)
        if source and _date_written(target, source["quote"]):
            out["milestones"].append({"name": normalize_ws(str(ms.get("name", ""))), "target_date": target,
                                      "status": ms.get("status", "planned"), **source})
        elif source:
            log_event(conn, log_id, 1, "monitor_item_dropped", input=ms, output="target date is not written in the quote")
    period_source = checked({"chunk_id": answer.get("period_chunk_id"), "quote": answer.get("period_quote")}) \
        if answer.get("period_end") else None
    if period_source and _date_written(str(answer["period_end"]), period_source["quote"]):
        out["period_end"] = answer["period_end"]
    return out


def _date_written(value: str, text: str) -> bool:
    """True if `value` is a valid ISO date written in `text` (as ISO, or in words like "June 30, 2026")."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return value in set(ISO_DATE.findall(text)) | set(iso_dates(text.lower()))


# --- 3. signals ----------------------------------------------------------------------------

def runway_months(period: dict) -> float | None:
    cash, burn = period["metrics"].get("cash"), period["metrics"].get("monthly_net_burn")
    if not cash or not burn or burn["value"] <= 0:
        return None
    return cash["value"] / burn["value"]


def revenue_growth_pct(period: dict, previous: dict | None) -> tuple[float | None, bool]:
    """(growth %, calculated?) from the two revenues if both are known, else as the update states it."""
    rev = period["metrics"].get("revenue")
    prev = previous and previous["metrics"].get("revenue")
    if rev and prev and prev["value"] > 0:
        return (rev["value"] / prev["value"] - 1) * 100, True
    stated = period["metrics"].get("revenue_growth_pct")
    return (stated["value"], False) if stated else (None, False)


def signals_for(periods: list[dict], company_id: int) -> list[dict]:
    """Every signal, per period, by rule. `periods` are sorted by period_end."""
    out = []
    milestones: dict[str, list[tuple[str, dict]]] = {}  # normalized name -> [(period_end, item)]
    for index, period in enumerate(periods):
        previous = periods[index - 1] if index else None
        end = period["period_end"]

        months = runway_months(period)
        if months is not None and months < RUNWAY_WARNING_MONTHS:
            growth, calculated = revenue_growth_pct(period, previous)
            high = growth is not None and growth < SLOW_GROWTH_PCT
            m = period["metrics"]
            detail = (f"{m['cash']['value_raw']} cash / {m['monthly_net_burn']['value_raw']} monthly net burn = "
                      f"{months:.1f} months of runway (calculated), under {RUNWAY_WARNING_MONTHS}.")
            if growth is None:
                detail += " Revenue growth is not stated, so the level stays at warning."
            else:
                how = "calculated from the two updates" if calculated else "as stated"
                detail += (f" Revenue growth {growth:.1f}% ({how})"
                           f"{f', under {SLOW_GROWTH_PCT}%' if high else ''}.")
            evidence = [m["cash"], m["monthly_net_burn"]] + [x for x in (m.get("revenue"), m.get("revenue_growth_pct"))
                                                              if x and high]
            out.append(_signal(company_id, end, "cash_runway", "high" if high else "warning",
                               f"Cash runway {months:.1f} months", detail, [round(months, 1)], evidence))

        if previous:
            listed = {p["name"].lower() for p in period["people"] if p["status"] == "present"}
            for person in previous["people"]:
                if person["status"] != "present" or not KEY_ROLE.search(person["role"]):
                    continue
                said = next((p for p in period["people"]
                             if p["name"].lower() == person["name"].lower() and p["status"] == "departed"), None)
                missing = period["people"] and person["name"].lower() not in listed
                if said or missing:
                    how = "stated as departed" if said else "no longer in the team list"
                    out.append(_signal(company_id, end, "key_person_departure", "high",
                                       f"{person['role']} {person['name']} departed",
                                       f"{person['name']} ({person['role']}) was listed in the previous update and is "
                                       f"{how} in this one.", [person["name"].lower()],
                                       [person] + ([said] if said else [])))

        for item in period["milestones"]:
            history = milestones.setdefault(item["name"].lower(), [])
            history.append((end, item))
            targets = [h[1]["target_date"] for h in history]
            slips = sum(1 for a, b in zip(targets, targets[1:]) if b > a)
            newly = slips >= SLIPS_FOR_SIGNAL and (len(targets) < 2 or targets[-1] > targets[-2])
            overdue = item["target_date"] < end and item["status"] != "done"
            if newly or overdue:
                what = (f"target date moved later {slips} times: " + " -> ".join(targets)) if newly \
                    else f"target date {item['target_date']} passed and it is not done"
                out.append(_signal(company_id, end, "milestone_slip", "warning",
                                   f"Milestone slipping: {item['name']}", f"{item['name']}: {what}.",
                                   [item["name"].lower(), *targets], [h[1] for h in history]))
    return out


def _signal(company_id, period_end, kind, level, title, detail, key_parts, evidence) -> dict:
    raw = "|".join([str(company_id), "signal", kind, period_end, *map(str, key_parts)])
    return {"period_end": period_end, "kind": kind, "level": level, "title": title, "detail": detail,
            "fingerprint": hashlib.sha256(raw.encode()).hexdigest()[:24], "evidence": evidence}


# --- the run --------------------------------------------------------------------------------

def update_documents(conn: sqlite3.Connection, company_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT d.id, d.company_id, d.filename, d.doc_type, d.doc_date, c.name AS company_name FROM documents d"
        " JOIN companies c ON c.id = d.company_id WHERE d.company_id = ? AND d.doc_type = 'notes'"
        " ORDER BY d.doc_date IS NULL, d.doc_date, d.id", (company_id,)).fetchall()


def create_run(conn: sqlite3.Connection, company_id: int) -> int:
    with conn:
        return conn.execute("INSERT INTO monitor_runs(company_id, status, stage) VALUES (?, 'running', 'Queued')",
                            (company_id,)).lastrowid


def run_monitor(conn: sqlite3.Connection, run_id: int, llm: LLMClient) -> None:
    """Do the work of a created run and store the result. Failures are stored on the run, not raised."""
    company_id = conn.execute("SELECT company_id FROM monitor_runs WHERE id = ?", (run_id,)).fetchone()["company_id"]
    log_id, notes = f"monitor-run{run_id}", []
    llm, started = CountingLLM(llm), time.monotonic()
    try:
        periods, people, milestones = [], [], []
        docs = update_documents(conn, company_id)
        for n, doc in enumerate(docs, 1):
            _set(conn, run_id, stage=f"Reading {doc['filename']} ({n} of {len(docs)})")
            found = extract_period(conn, doc, llm, people, milestones, log_id)
            if not any(k in found["metrics"] for k in PERIOD_KPIS):
                notes.append(f"{doc['filename']} reports no cash, burn or revenue, so it is not treated as an update.")
                continue
            if not found["period_end"]:
                if not doc["doc_date"]:
                    notes.append(f"{doc['filename']} has no period date and no material date; skipped.")
                    continue
                found["period_end"] = doc["doc_date"]
                notes.append(f"{doc['filename']} states no period date; its material date is used.")
            periods.append(found | {"document_id": doc["id"], "filename": doc["filename"]})
            people += [p["name"] for p in found["people"] if p["name"] not in people]
            milestones += [m["name"] for m in found["milestones"] if m["name"] not in milestones]
        periods.sort(key=lambda p: p["period_end"])
        signals = signals_for(periods, company_id)
        with conn:
            for p in periods:
                conn.execute("INSERT INTO monitor_periods(run_id, company_id, document_id, period_end, data_json)"
                             " VALUES (?, ?, ?, ?, ?)", (run_id, company_id, p["document_id"], p["period_end"],
                                                         json.dumps(p)))
            for s in signals:
                conn.execute("INSERT INTO signals(run_id, company_id, period_end, kind, level, title, detail,"
                             " fingerprint, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (run_id, company_id, s["period_end"], s["kind"], s["level"], s["title"], s["detail"],
                              s["fingerprint"], json.dumps(s["evidence"])))
            _set(conn, run_id, status="done", notes=notes)
    except LLMError as e:
        _set(conn, run_id, status="failed", error=f"Model call failed: {e}", notes=notes)
    except Exception as e:  # noqa: BLE001 (the run must end in a stored state the page can show)
        log.exception("Monitor run %s failed", run_id)
        _set(conn, run_id, status="failed", error=f"Monitoring failed ({type(e).__name__}: {e}).", notes=notes)
    finally:
        log_event(conn, log_id, 0, "usage", output=llm.summary() | {"seconds": round(time.monotonic() - started, 1)})


def _set(conn: sqlite3.Connection, run_id: int, stage: str | None = None, status: str | None = None,
         error: str | None = None, notes: list[str] | None = None) -> None:
    with conn:
        if status:
            conn.execute("UPDATE monitor_runs SET status = ?, stage = NULL, error = ?, notes_json = ?,"
                         " finished_at = CURRENT_TIMESTAMP WHERE id = ?", (status, error, json.dumps(notes or []), run_id))
        else:
            conn.execute("UPDATE monitor_runs SET stage = ? WHERE id = ?", (stage, run_id))


def fail_interrupted_runs(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute("UPDATE monitor_runs SET status = 'failed', stage = NULL, finished_at = CURRENT_TIMESTAMP,"
                            " error = 'The server restarted during this run. Run it again.'"
                            " WHERE status = 'running'").rowcount


_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor")


def start_monitor(db_path: Path | str, company_id: int, llm: LLMClient) -> int:
    conn = connect(db_path)
    try:
        run_id = create_run(conn, company_id)
    finally:
        conn.close()
    _worker.submit(_run_in_background, db_path, run_id, llm)
    return run_id


def _run_in_background(db_path, run_id: int, llm: LLMClient) -> None:
    conn = connect(db_path)
    try:
        run_monitor(conn, run_id, llm)
    finally:
        conn.close()


# --- reading results ------------------------------------------------------------------------

def get_run(conn: sqlite3.Connection, company_id: int, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM monitor_runs WHERE id = ? AND company_id = ?", (run_id, company_id)).fetchone()
    if not row:
        return None
    usage = conn.execute("SELECT output_json FROM agent_logs WHERE run_id = ? AND kind = 'usage' ORDER BY id DESC LIMIT 1",
                         (f"monitor-run{run_id}",)).fetchone()
    return {k: row[k] for k in ("id", "company_id", "status", "stage", "error", "started_at", "finished_at")} | {
        "notes": json.loads(row["notes_json"] or "[]"), "usage": usage and json.loads(usage["output_json"])}


def latest_results(conn: sqlite3.Connection, company_id: int) -> dict:
    def newest(status):
        row = conn.execute("SELECT id FROM monitor_runs WHERE company_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                           (company_id, status)).fetchone()
        return row and row["id"]

    running, last, failed = newest("running"), newest("done"), newest("failed")
    result = {"running": running and get_run(conn, company_id, running), "run": last and get_run(conn, company_id, last),
              "failed_run": get_run(conn, company_id, failed) if failed and (not last or failed > last) else None,
              "periods": [], "signals": []}
    if last:
        periods = [json.loads(r["data_json"]) for r in conn.execute(
            "SELECT data_json FROM monitor_periods WHERE run_id = ? AND company_id = ? ORDER BY period_end, id",
            (last, company_id))]
        for i, p in enumerate(periods):
            months = runway_months(p)
            growth, calculated = revenue_growth_pct(p, periods[i - 1] if i else None)
            p["runway_months"] = months and round(months, 1)
            p["revenue_growth_pct"] = None if growth is None else round(growth, 1)
            p["revenue_growth_calculated"] = calculated
        result["periods"] = periods
        result["signals"] = [dict(r) | {"evidence": json.loads(r["evidence_json"])} for r in conn.execute(
            "SELECT period_end, kind, level, title, detail, fingerprint, evidence_json FROM signals"
            " WHERE run_id = ? AND company_id = ? ORDER BY period_end, id", (last, company_id))]
        for s in result["signals"]:
            s.pop("evidence_json")
    return result
