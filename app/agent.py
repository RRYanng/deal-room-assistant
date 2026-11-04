"""Q&A agent: a Claude tool-use loop over four read-only tools, then program-side checks.

One question:
  1. user question -> model; the model calls tools (search, metrics, conflicts, CRM)
  2. tools run with the company_id chosen in the UI (injected by the server; the model
     never chooses the company), each call is logged
  3. loop until the model gives its final JSON answer or MAX steps is reached
  4. the program keeps only citations whose quote is verbatim in a chunk of this company;
     claims left without a valid citation are dropped (and logged)
  5. program-detected conflicts / unclear-basis metrics touching the cited chunks are attached
"""

import json
import re
import sqlite3
import uuid
from typing import Any

from app.citations import verify_citation
from app.config import get_settings
from app.llm import LLMClient, ToolResult, ToolSpec
from app.logs import log_event
from app.rag import search
from app.rules import find_conflicts, get_metrics, unclear_basis

NOT_FOUND = "Not found in the provided materials"
LAST_STEP_NOTE = ("You have one step left. Do not call any more tools. Reply now with only the final "
                  "JSON answer, based on what you have found; list anything unresolved as open items.")

SYSTEM_PROMPT = f"""You are a research assistant for a venture capital investment team.
You answer questions about ONE selected company (named in the user message), using only its
materials, which you can reach through the tools.

How to work:
- Use the tools before answering. search_documents finds passages; get_metrics returns
  extracted metrics with their verbatim quotes; get_conflicts returns contradictions found
  by the system; get_crm_history returns the communication log, oldest first.
- Never use outside knowledge or guess.
- Be efficient: if two or three searches find nothing relevant, stop searching and report
  it as an open item.

Rules for the answer:
- Each claim is one short factual sentence supported by the materials.
- Each claim must cite at least one source: the chunk_id and a quote copied character for
  character from that chunk's text (or the quote field of a metric). Do not paraphrase quotes.
- Keep metrics separate: registered customers are not paying customers; never present one as
  the other.
- If sources disagree, state every value with its source in separate claims. Do not pick one.
- If the question is about a different company than the selected one, do not answer it with the
  selected company's data. Write no claims; add one open item saying the question is about another
  company and only the selected company's materials are available.
- If something asked is not in the materials, do not write a claim for it. Add an open item
  in the form "<topic>: {NOT_FOUND}".

When you are done, reply with only this JSON object and nothing else:
{{"claims": [{{"text": "...", "citations": [{{"chunk_id": 123, "quote": "..."}}]}}],
 "open_items": ["..."]}}"""

TOOLS = [
    ToolSpec("search_documents",
             "Hybrid keyword + semantic search in the selected company's materials. Returns the most "
             "relevant passages with chunk_id, location and full text.",
             {"type": "object", "properties": {"query": {"type": "string", "description": "What to look for."}},
              "required": ["query"]}),
    ToolSpec("get_metrics",
             "Structured metrics extracted from the company's materials (registered_customers, "
             "paying_customers, revenue, funding_raised, other), each with value, as-of date, "
             "chunk_id and verbatim quote.",
             {"type": "object", "properties": {"metric": {
                 "type": "string", "description": "Optional: only this metric.",
                 "enum": ["registered_customers", "paying_customers", "revenue", "funding_raised", "other"]}}}),
    ToolSpec("get_conflicts",
             "Contradictions detected by the system (same metric, same as-of date, different values), "
             "with every value and source, plus metrics whose basis is unclear (no as-of date).",
             {"type": "object", "properties": {}}),
    ToolSpec("get_crm_history",
             "The CRM communication log for the company, ordered by date (oldest first), with "
             "contact, summary, next steps, chunk_id and the row text.",
             {"type": "object", "properties": {}}),
]


# --- tools (company_id always comes from the server) ---------------------------------

def run_tool(conn: sqlite3.Connection, company_id: int, name: str, args: dict[str, Any]) -> Any:
    if name == "search_documents":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        return [{k: r[k] for k in ("chunk_id", "loc_label", "filename", "doc_type", "text")}
                for r in search(conn, company_id, query)]
    if name == "get_metrics":
        keys = ("metric", "metric_label", "value", "value_raw", "unit", "currency", "as_of_date",
                "chunk_id", "quote", "loc_label", "filename")
        return [{k: m[k] for k in keys} for m in get_metrics(conn, company_id, args.get("metric") or None)]
    if name == "get_conflicts":
        return {"conflicts": find_conflicts(conn, company_id), "unclear_basis": unclear_basis(conn, company_id)}
    if name == "get_crm_history":
        rows = conn.execute(
            "SELECT r.date, r.contact, r.summary, r.next_steps, r.chunk_id, ch.text, ch.loc_label"
            " FROM crm_records r JOIN chunks ch ON ch.id = r.chunk_id"
            " WHERE r.company_id = ? ORDER BY r.date, r.row_num", (company_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    raise ValueError(f"unknown tool {name!r}")


# --- the loop ---------------------------------------------------------------------

def run_agent(conn: sqlite3.Connection, company_id: int, user_prompt: str, llm: LLMClient,
              system: str = SYSTEM_PROMPT, max_steps: int | None = None, run_id: str | None = None) -> dict:
    """Run the tool loop and return {"final": parsed JSON or None, ...}. Raises LLMError on API failure."""
    max_steps = max_steps or get_settings().agent_max_steps
    run_id = run_id or uuid.uuid4().hex[:12]
    messages = [llm.user_message(user_prompt)]
    tool_calls: list[dict] = []
    log_event(conn, run_id, 0, "question", input={"company_id": company_id, "prompt": user_prompt})

    final, retried, status = None, False, "step_limit"
    for step in range(1, max_steps + 1):
        response = llm.chat(system, messages, TOOLS)
        messages.append(response.assistant_message)

        if response.stop_reason == "refusal":
            status = "refused"
            break
        if response.tool_calls:
            results = []
            for call in response.tool_calls:
                args = dict(call.input)
                if "company_id" in args:  # the model may not choose the company
                    log_event(conn, run_id, step, "company_id_ignored", tool=call.name, input=args)
                    args.pop("company_id")
                try:
                    output = run_tool(conn, company_id, call.name, args)
                    results.append(ToolResult(call.id, json.dumps(output, default=str)))
                except Exception as e:  # a tool error goes back to the model, it can recover
                    output = {"error": str(e)}
                    results.append(ToolResult(call.id, json.dumps(output), is_error=True))
                tool_calls.append({"step": step, "tool": call.name, "input": args})
                log_event(conn, run_id, step, "tool_call", tool=call.name, input=args, output=output)
            note = LAST_STEP_NOTE if step == max_steps - 1 else None
            messages.append(llm.tool_results_message(results, note))
            continue

        final = parse_json_object(response.text)
        if final is not None:
            status = "ok"
            break
        log_event(conn, run_id, step, "unparseable_answer", output=response.text[:2000])
        if retried:
            status = "unparseable"
            break
        retried = True
        messages.append(llm.user_message("Reply with only the JSON object in the required format."))

    log_event(conn, run_id, max_steps, "loop_end", output={"status": status})
    return {"run_id": run_id, "status": status, "final": final, "tool_calls": tool_calls}


def parse_json_object(text: str) -> dict | None:
    """Parse the model's final answer; tolerates code fences or text around the object."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    if "{" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


# --- program-side citation checks -------------------------------------------------

def verify_items(conn: sqlite3.Connection, company_id: int, run_id: str, items: list, kind: str) -> list[dict]:
    """Keep items (claims / brief bullets) whose citations check out; drop and log the rest."""
    kept = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        evidence = []
        for cit in item.get("citations") or []:
            cit = cit if isinstance(cit, dict) else {}
            row = verify_citation(conn, company_id, cit.get("chunk_id"), str(cit.get("quote", "")))
            if row is None:
                log_event(conn, run_id, 0, "citation_dropped", input={"claim": item.get("text"), "citation": cit},
                          output="quote not verbatim in a chunk of this company")
                continue
            evidence.append({"chunk_id": row["id"], "quote": " ".join(str(cit["quote"]).split()),
                             "loc_label": row["loc_label"], "filename": row["filename"],
                             "doc_type": row["doc_type"], "doc_date": row["doc_date"], "chunk_text": row["text"]})
        if evidence:
            kept.append({"text": str(item["text"]).strip(), "citations": evidence})
        else:
            log_event(conn, run_id, 0, f"{kind}_dropped", input=item, output="no valid citation")
    return kept


def related_checks(conn: sqlite3.Connection, company_id: int, chunk_ids: set[int]) -> dict:
    """Program-detected conflicts and unclear-basis metrics that involve any of the cited chunks."""
    conflicts = [c for c in find_conflicts(conn, company_id)
                 if any(s["chunk_id"] in chunk_ids for s in c["sources"])]
    unclear = [u for u in unclear_basis(conn, company_id) if u["chunk_id"] in chunk_ids]
    return {"conflicts": conflicts, "unclear_basis": unclear}


def group_evidence(claims: list[dict]) -> list[dict]:
    """Group claims linked by a shared chunk, so the UI shows each source passage only once.

    A display structure only: each claim keeps its own verified citations. A group sits at the
    position of its first claim. `claims` values are indexes into the answer's claims.
    """
    groups: list[tuple[list[int], set[int]]] = []
    for index, claim in enumerate(claims):
        chunk_ids = {c["chunk_id"] for c in claim["citations"]}
        touching = [g for g in groups if g[1] & chunk_ids]
        if not touching:
            groups.append(([index], chunk_ids))
            continue
        members, ids = touching[0]
        for other in touching[1:]:
            members += other[0]
            ids |= other[1]
            groups.remove(other)
        members.append(index)
        ids |= chunk_ids

    result = []
    for members, _ in groups:
        members.sort()
        evidence: dict[int, dict] = {}
        for index in members:
            for c in claims[index]["citations"]:
                entry = evidence.setdefault(c["chunk_id"], {
                    "chunk_id": c["chunk_id"], "filename": c["filename"], "loc_label": c["loc_label"],
                    "doc_date": c["doc_date"], "chunk_text": c["chunk_text"], "quotes": [], "claims": []})
                if c["quote"] not in entry["quotes"]:
                    entry["quotes"].append(c["quote"])
                if index not in entry["claims"]:
                    entry["claims"].append(index)
        result.append({"claims": members, "evidence": list(evidence.values())})
    return result


def answer_question(conn: sqlite3.Connection, company_id: int, question: str, llm: LLMClient) -> dict:
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()["name"]
    run = run_agent(conn, company_id, f"Selected company: {company}\nQuestion: {question}", llm)
    final = run["final"] or {}
    claims = verify_items(conn, company_id, run["run_id"], final.get("claims"), "claim")
    open_items = [str(o).strip() for o in final.get("open_items") or [] if str(o).strip()]

    if run["status"] == "step_limit":
        open_items.append(f"Step limit reached before a final answer; {NOT_FOUND.lower()} so far.")
    elif run["status"] == "refused":
        open_items.append("The model declined to answer this question.")
    elif run["status"] == "unparseable":
        open_items.append("The model's answer could not be read; please ask again.")
    if not claims and not open_items:
        open_items.append(f"{question.strip()}: {NOT_FOUND}")

    cited = {c["chunk_id"] for claim in claims for c in claim["citations"]}
    result = {"run_id": run["run_id"], "status": run["status"], "question": question,
              "claims": claims, "evidence_groups": group_evidence(claims),
              "open_items": open_items, "tool_calls": run["tool_calls"]}
    result |= related_checks(conn, company_id, cited)
    log_event(conn, run["run_id"], 0, "final", output={"claims": len(claims), "open_items": open_items})
    return result
