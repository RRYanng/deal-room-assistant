"""One-click company brief.

Two kinds of content, kept apart on purpose:
  - Program-built (deterministic): key metrics table, recent communications (CRM),
    missing key metrics, conflicts, unclear-basis metrics. Each row carries its source.
  - Model-written: 2-4 short sections that follow the user's requirements
    (e.g. "focus on traction and risks"), produced by the same tool loop as Q&A.
    Every bullet must cite a verbatim quote; bullets that fail the check are dropped.
"""

import re
import sqlite3
from datetime import datetime, timezone

from app.agent import NOT_FOUND, run_agent, verify_items
from app.citations import normalize_ws
from app.llm import LLMClient
from app.logs import log_event
from app.rules import KEY_METRICS, find_conflicts, get_metrics, missing_metrics, unclear_basis

RECENT_CRM_ROWS = 3
_DERIVED_FIGURE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s?(?:%|x\b|percent\b|times\b)", re.IGNORECASE)
DEFAULT_REQUIREMENTS = "General overview: traction, recent communications, risks and open questions."

BRIEF_SYSTEM_PROMPT = f"""You write a one-page company brief for a venture capital investment team.
Use only the company's materials, which you reach through the tools. Never use outside knowledge.

Follow the user's requirements for what to focus on. Write 2 to 4 sections, each with a short
title and 1 to 5 bullets. Each bullet is one factual sentence and must cite at least one source:
the chunk_id and a quote copied character for character from that chunk's text (or the quote
field of a metric). Keep metrics separate: registered customers are not paying customers.
If sources disagree, state each value with its source; do not pick one.
Never mention chunk ids in the text of a bullet or open item.
Do not speculate beyond the materials: a risk is something the materials show, or a gap
they leave ("X is not stated"), never a prediction.

You may do a calculation only if it is one simple step on figures stated in the materials (a ratio,
percentage, difference or sum) that a reader can check by hand. Show the working in the bullet, as in
"60% of registered businesses pay (60 / 100)", cite the source of every figure, and set "calculated"
to true on that bullet. Do not estimate, forecast or chain calculations, and do not calculate from a
figure the sources disagree on.

The system automatically adds a key-metrics table, recent communications, missing information
and conflicts to the brief, so you do not need to list those mechanically; use your sections for
analysis that follows the requirements. Anything relevant you could not find goes into
open_items as "<topic>: {NOT_FOUND}".

Be efficient: a few tool calls are enough. When done, reply with only this JSON object:
{{"sections": [{{"title": "...", "items": [{{"text": "...", "calculated": false, "citations": [{{"chunk_id": 123, "quote": "..."}}]}}]}}],
 "open_items": ["..."]}}"""


def generate_brief(conn: sqlite3.Connection, company_id: int, requirements: str, llm: LLMClient) -> dict:
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()["name"]
    requirements = requirements.strip() or DEFAULT_REQUIREMENTS
    prompt = f"Write the brief for {company}.\nRequirements from the investment team: {requirements}"

    run = run_agent(conn, company_id, prompt, llm, system=BRIEF_SYSTEM_PROMPT)
    final = run["final"] or {}
    sections = []
    for section in final.get("sections") or []:
        if not isinstance(section, dict):
            continue
        items = verify_items(conn, company_id, run["run_id"], section.get("items"), "brief_item")
        if items:
            sections.append({"title": str(section.get("title") or "Notes").strip(), "items": items})
    open_items = [str(o).strip() for o in final.get("open_items") or [] if str(o).strip()]
    if run["status"] != "ok":
        open_items.append(f"The written sections are incomplete (agent status: {run['status']}).")
    conflicts = find_conflicts(conn, company_id)
    mark_traction_conflicts(sections, conflicts)
    mark_calculated(sections)

    brief = {
        "run_id": run["run_id"],
        "company": company,
        "requirements": requirements,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sections": sections,
        "key_metrics": key_metrics(conn, company_id),
        "recent_communications": recent_communications(conn, company_id),
        "missing": [{"metric": m, "text": f"{m}: {NOT_FOUND}"} for m in missing_metrics(conn, company_id)],
        "conflicts": conflicts,
        "unclear_basis": unclear_basis(conn, company_id),
        "open_items": open_items,
        "tool_calls": run["tool_calls"],
    }
    log_event(conn, run["run_id"], 0, "brief_done",
              output={"sections": len(sections), "missing": [m["metric"] for m in brief["missing"]],
                      "conflicts": len(brief["conflicts"])})
    return brief


def _quotes_value(quote: str, value_raw: str) -> bool:
    """True if the quote states this value as a whole number or word: "60" is not in "2060" or "60.5"."""
    pattern = rf"(?<![\w.,]){re.escape(normalize_ws(value_raw))}(?!\w|[.,]\d)"
    return re.search(pattern, quote, re.IGNORECASE) is not None


def _cites_conflict(item: dict, conflicts: list[dict]) -> bool:
    """True if a citation quotes a conflicting value from the chunk that value was extracted from."""
    return any(c["chunk_id"] == s["chunk_id"] and _quotes_value(c["quote"], s["value_raw"])
               for c in item["citations"] for conflict in conflicts for s in conflict["sources"])


def mark_traction_conflicts(sections: list[dict], conflicts: list[dict]) -> None:
    """Add a note to Traction bullets that state a conflicting value.

    The note points to Risks only if a Risks section covers the conflict. Otherwise it points to
    the program-built "Conflicting information" section, which always lists the conflict.
    """
    def title(section):
        return section["title"].lower()

    risks_cover = any("risk" in title(s) and "traction" not in title(s)
                      and any(_cites_conflict(i, conflicts) for i in s["items"]) for s in sections)
    target = "Risks" if risks_cover else "Conflicting information"
    for section in sections:
        if "traction" in title(section):
            for item in section["items"]:
                if _cites_conflict(item, conflicts):
                    item["note"] = f"(conflicting — see {target})"


def mark_calculated(sections: list[dict]) -> None:
    """Catch calculated figures the model did not flag: a percentage or multiple not in any cited passage."""
    for section in sections:
        for item in section["items"]:
            sources = " ".join(c["chunk_text"] for c in item["citations"])
            for number in _DERIVED_FIGURE.findall(item["text"]):
                if not re.search(rf"(?<![\d.]){re.escape(number)}\s?(?:%|x\b|percent\b|times\b)", sources, re.IGNORECASE):
                    item["calculated"] = True


def key_metrics(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    """Key metrics with a stated as-of date, each with its source. Unclear ones are listed separately."""
    keys = ("metric", "value", "value_raw", "display_value", "unit", "currency", "as_of_date", "quote", "chunk_id",
            "loc_label", "filename")
    return [{k: m[k] for k in keys} for m in get_metrics(conn, company_id)
            if m["metric"] in KEY_METRICS and m["as_of_date"] and m["value"] is not None]


def recent_communications(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT r.date, r.contact, r.summary, r.next_steps, r.chunk_id, ch.text, ch.loc_label, d.filename"
        " FROM crm_records r JOIN chunks ch ON ch.id = r.chunk_id JOIN documents d ON d.id = r.document_id"
        " WHERE r.company_id = ? ORDER BY r.date DESC, r.row_num DESC LIMIT ?",
        (company_id, RECENT_CRM_ROWS),
    ).fetchall()
    return [{"date": r["date"], "contact": r["contact"], "summary": r["summary"], "next_steps": r["next_steps"],
             "chunk_id": r["chunk_id"], "quote": r["text"], "loc_label": r["loc_label"], "filename": r["filename"]}
            for r in rows]


def brief_to_markdown(brief: dict) -> str:
    """Plain Markdown version for copy / download. Every line keeps its source in brackets."""
    lines = [f"# Company brief: {brief['company']}", "",
             f"_Requirements: {brief['requirements']}_  ", f"_Generated: {brief['generated_at']}_", ""]
    for section in brief["sections"]:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            sources = "; ".join(f"{c['loc_label']}: \"{c['quote']}\"" for c in item["citations"])
            notes = (" (calculated)" if item.get("calculated") else "") + (f" {item['note']}" if item.get("note") else "")
            lines.append(f"- {item['text']}{notes} [{sources}]")
        lines.append("")
    lines.append("## Key metrics")
    for m in brief["key_metrics"]:
        lines.append(f"- {m['metric']}: {m['display_value']} as of {m['as_of_date']} [{m['loc_label']}: \"{m['quote']}\"]")
    if not brief["key_metrics"]:
        lines.append("- No key metric with a stated as-of date.")
    lines += ["", "## Recent communications"]
    for c in brief["recent_communications"]:
        lines.append(f"- {c['date']} with {c['contact']}: {c['summary']} Next: {c['next_steps']} [{c['loc_label']}]")
    if not brief["recent_communications"]:
        lines.append(f"- CRM history: {NOT_FOUND}")
    lines += ["", "## Missing information"]
    lines += [f"- {m['text']}" for m in brief["missing"]] or ["- None of the key metrics are missing."]
    lines += ["", "## Conflicting information"]
    for c in brief["conflicts"]:
        lines.append(f"- {c['metric']} as of {c['as_of_date']}: values differ, not resolved:")
        lines += [f"  - {s['display_value']} [{s['loc_label']}: \"{s['quote']}\"]" for s in c["sources"]]
    if not brief["conflicts"]:
        lines.append("- No conflicts detected.")
    if brief["unclear_basis"]:
        lines += ["", "## Unclear basis"]
        lines += [f"- {u['metric_label'] or u['metric']}: {u['display_value']} ({u['reason']}) "
                  f"[{u['loc_label']}: \"{u['quote']}\"]" for u in brief["unclear_basis"]]
    if brief["open_items"]:
        lines += ["", "## Open items"] + [f"- {o}" for o in brief["open_items"]]
    return "\n".join(lines) + "\n"
