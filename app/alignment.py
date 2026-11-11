"""Alignment check: who pays, how the company charges, who benefits, and where those could pull apart.

One model call per claim check (skills/alignment-check/SKILL.md) returns quotes for payer, pricing and
beneficiaries, and tension points of fixed types. The program keeps an item only if its quote is
verbatim in a chunk of the deck, and writes each point's neutral note and its question from the type.
Nothing here concludes that a business model is misaligned: a point is a question for the founder.
"""

import sqlite3

from app.citations import normalize_ws, quote_in_text
from app.llm import LLMClient
from app.logs import log_event
from app.questions import verified_question
from app.skills import skill_prompt

ROLES = ("payer", "pricing", "beneficiary")
# Fixed tension types: (title, neutral note, question). {quote} and {page} are filled in by the program.
TENSIONS = {
    "share_of_recovered": (
        "Part of the price is a share of amounts recovered",
        "Fees tied to amounts recovered are common in revenue-cycle services and are not a problem in themselves. "
        "The point to understand is how the incentive to recover more stays in line with the customer's and the "
        "patient's interest.",
        "The deck says: \"{quote}\" ({page}). How are claims chosen for resubmission, and who decides that a denied "
        "claim should not be pursued?"),
    "per_transaction": (
        "The price rises with volume",
        "Volume-based pricing is common. The point to understand is what keeps volume in line with what the "
        "customer needs.",
        "The deck says: \"{quote}\" ({page}). What keeps the volume billed in line with what the customer needs?"),
    "payer_not_beneficiary": (
        "The party that pays may not be the party that benefits most",
        "This is common when a business serves several parties. The point to understand is where their interests "
        "could differ.",
        "The deck says: \"{quote}\" ({page}). How do the interests of the party that pays and the party that "
        "benefits line up, and where could they differ?"),
    "third_party_revenue": (
        "Revenue from a party on the other side of the customer",
        "Two-sided revenue is common. The point to understand is how a conflict with customers is handled.",
        "The deck says: \"{quote}\" ({page}). Which parties pay the company, and how is a conflict with customers "
        "handled?"),
}

_quote = {"chunk_id": {"type": "integer"}, "quote": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "roles": {"type": "array", "items": {"type": "object", "properties": {
            "role": {"type": "string", "enum": list(ROLES)}, **_quote},
            "required": ["role", "chunk_id", "quote"], "additionalProperties": False}},
        "tensions": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": list(TENSIONS)}, **_quote},
            "required": ["type", "chunk_id", "quote"], "additionalProperties": False}},
    },
    "required": ["roles", "tensions"],
    "additionalProperties": False,
}


def read_business_model(conn: sqlite3.Connection, deck: sqlite3.Row, llm: LLMClient, log_id: str) -> list[dict]:
    """Checked roles and tension points of the deck. Raises LLMError on model failure."""
    chunks = conn.execute("SELECT id, text, loc_label FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                          (deck["id"],)).fetchall()
    by_id = {ch["id"]: ch for ch in chunks}
    prompt = "\n".join([f"Deck: {deck['filename']}", "", "Chunks:"] +
                       [f"[chunk_id={ch['id']}] ({ch['loc_label']})\n{ch['text']}\n" for ch in chunks])
    answer = llm.complete_json(skill_prompt("alignment-check", "Read the business model"), prompt, SCHEMA)
    items, seen = [], set()
    for kind, rows in (("role", answer.get("roles") or []), ("tension", answer.get("tensions") or [])):
        for item in rows:
            chunk = by_id.get(item.get("chunk_id")) if isinstance(item, dict) else None
            quote = normalize_ws(str((item or {}).get("quote", "")))
            label = item.get("role") if kind == "role" else item.get("type")
            if chunk is None or not quote_in_text(quote, chunk["text"]) or label not in (ROLES if kind == "role" else TENSIONS):
                log_event(conn, log_id, 1, "alignment_dropped", input=item, output="quote not verbatim, or unknown type")
                continue
            if (kind, label, quote) not in seen:
                seen.add((kind, label, quote))
                items.append({"kind": kind, "label": label, "chunk_id": chunk["id"], "quote": quote,
                              "loc_label": chunk["loc_label"]})
    return items


def save(conn: sqlite3.Connection, run_id: int, company_id: int, items: list[dict]) -> None:
    for i in items:
        conn.execute("INSERT INTO alignment_items(run_id, company_id, kind, label, chunk_id, quote, loc_label)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, company_id, i["kind"], i["label"], i["chunk_id"], i["quote"], i["loc_label"]))


def results(conn: sqlite3.Connection, company_id: int, run_id: int, filename: str) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, label, quote, loc_label FROM alignment_items WHERE run_id = ? AND company_id = ? ORDER BY id",
        (run_id, company_id))]
    out = {role: [r for r in rows if r["kind"] == "role" and r["label"] == role] for role in ROLES}
    points = []
    for r in rows:
        if r["kind"] != "tension":
            continue
        title, note, question = TENSIONS[r["label"]]
        page = (r["loc_label"] or "").replace(filename, "").strip()
        points.append(r | {"title": title, "note": note,
                           "question": verified_question(question.format(quote=r["quote"], page=page), [r["quote"]])})
    return out | {"points": points}
