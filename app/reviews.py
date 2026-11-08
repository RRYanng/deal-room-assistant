"""Investor review of flagged items: confirm or dismiss, always with a reason.

Nothing the system flags is a conclusion until an investor confirms it. Rules, all in code:
  - a decision is stored by fingerprint (company + type + fixed key + numbers), so it carries over to
    later runs of the same claim, and never by model wording;
  - the latest decision wins (the table is append-only, so the history stays);
  - a dismissed item is not raised again, unless its evidence changes: the decision remembers the status
    and an evidence signature, and when either differs the item is back under "Awaiting review";
  - the same applies to a confirmed item, so the memo never presents changed evidence as reviewed.
"""

import hashlib
import json
import sqlite3

from app.citations import normalize_ws
from app.claims import run_claims

DECISIONS = ("confirmed", "dismissed")
ITEM_TYPES = ("claim",)  # 'signal' and 'drift' arrive with monitoring and memory
FLAG_STATUSES = ("contradicted", "possibly_overstated", "unverified")


class ReviewError(ValueError):
    """The review cannot be stored; the message is shown to the user as-is."""


def evidence_signature(claim: dict) -> str:
    """Stable summary of a claim's evidence: what kind, which way, which number, which passage."""
    parts = sorted(f"{e['source']}|{e['stance']}|{normalize_ws(e.get('value_raw') or '')}|"
                   f"{normalize_ws(e.get('quote') or e.get('note') or '').lower()}" for e in claim["evidence"])
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def record_review(conn: sqlite3.Connection, company_id: int, fingerprint: str, decision: str, reason: str,
                  item_type: str = "claim") -> dict:
    if item_type not in ITEM_TYPES:
        raise ReviewError(f"Unknown item type '{item_type}'.")
    if decision not in DECISIONS:
        raise ReviewError("Decision must be 'confirmed' or 'dismissed'.")
    reason = normalize_ws(reason or "")
    if not reason:
        raise ReviewError("A reason is required.")
    claim = _latest_claim(conn, company_id, fingerprint)
    if claim is None:  # also refuses a fingerprint of another company
        raise ReviewError("No such item for this company.")
    snapshot = {"status": claim["status"], "evidence_signature": evidence_signature(claim),
                "quote": claim["quote"], "key": claim["key"]}
    with conn:
        review_id = conn.execute(
            "INSERT INTO reviews(company_id, item_type, fingerprint, decision, reason, item_json) VALUES (?, ?, ?, ?, ?, ?)",
            (company_id, item_type, fingerprint, decision, reason, json.dumps(snapshot))).lastrowid
    return dict(conn.execute("SELECT id, fingerprint, decision, reason, created_at FROM reviews WHERE id = ?",
                             (review_id,)).fetchone())


def latest_decisions(conn: sqlite3.Connection, company_id: int, item_type: str = "claim") -> dict[str, dict]:
    rows = conn.execute("SELECT fingerprint, decision, reason, item_json, created_at FROM reviews"
                        " WHERE company_id = ? AND item_type = ? ORDER BY id", (company_id, item_type)).fetchall()
    out = {}
    for r in rows:  # later rows overwrite earlier ones: the latest decision wins
        out[r["fingerprint"]] = {"decision": r["decision"], "reason": r["reason"], "created_at": r["created_at"],
                                 "seen": json.loads(r["item_json"] or "{}")}
    return out


def apply_reviews(conn: sqlite3.Connection, company_id: int, claims: list[dict]) -> list[dict]:
    """Add `review` and `review_state` to each claim: confirmed, dismissed, or awaiting review."""
    decisions = latest_decisions(conn, company_id)
    for claim in claims:
        review = decisions.get(claim["fingerprint"])
        claim["review"], claim["evidence_changed"] = None, False
        if review is None:
            claim["review_state"] = "awaiting_review"
            continue
        seen = review.pop("seen")
        changed = seen.get("status") != claim["status"] or seen.get("evidence_signature") != evidence_signature(claim)
        claim["review"] = review | {"status_then": seen.get("status")}
        claim["evidence_changed"] = changed
        claim["review_state"] = "awaiting_review" if changed else review["decision"]
    return claims


def review_counts(claims: list[dict]) -> dict:
    awaiting = [c for c in claims if c["review_state"] == "awaiting_review"]
    return {"awaiting_review": len(awaiting),
            "flags_awaiting_review": sum(c["status"] in FLAG_STATUSES for c in awaiting),
            "confirmed": sum(c["review_state"] == "confirmed" for c in claims),
            "dismissed": sum(c["review_state"] == "dismissed" for c in claims)}


def _latest_claim(conn: sqlite3.Connection, company_id: int, fingerprint: str) -> dict | None:
    """The claim with this fingerprint in the company's newest finished run, with its evidence."""
    run = conn.execute("SELECT r.id FROM claim_runs r JOIN claims c ON c.run_id = r.id WHERE r.company_id = ?"
                       " AND r.status = 'done' AND c.fingerprint = ? ORDER BY r.id DESC LIMIT 1",
                       (company_id, fingerprint)).fetchone()
    if not run:
        return None
    return next((c for c in run_claims(conn, company_id, run["id"]) if c["fingerprint"] == fingerprint), None)
