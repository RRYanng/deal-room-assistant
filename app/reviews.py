"""Investor review of flagged items: confirm or dismiss, always with a reason.

Nothing the system flags is a conclusion until an investor confirms it. Claims (claim check) and
signals (portfolio monitoring) follow the same rules, all in code:
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
from app.monitor import latest_results as latest_monitor_results

DECISIONS = ("confirmed", "dismissed")
ITEM_TYPES = ("claim", "signal")
FLAG_STATUSES = ("contradicted", "possibly_overstated", "unverified")


class ReviewError(ValueError):
    """The review cannot be stored; the message is shown to the user as-is."""


def evidence_signature(claim: dict) -> str:
    """Stable summary of a claim's evidence: what kind, which way, which number, which passage."""
    parts = sorted(f"{e['source']}|{e['stance']}|{normalize_ws(e.get('value_raw') or '')}|"
                   f"{normalize_ws(e.get('quote') or e.get('note') or '').lower()}" for e in claim["evidence"])
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def signal_signature(signal: dict) -> str:
    """Stable summary of a signal's basis: its calculation and the quotes it rests on."""
    parts = [normalize_ws(signal["detail"])] + sorted(normalize_ws(e.get("quote") or "").lower()
                                                       for e in signal["evidence"])
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _state(item: dict, item_type: str) -> tuple[str, str]:
    """What the investor saw: (status or level, evidence signature)."""
    if item_type == "signal":
        return item["level"], signal_signature(item)
    return item["status"], evidence_signature(item)


def record_review(conn: sqlite3.Connection, company_id: int, fingerprint: str, decision: str, reason: str,
                  item_type: str = "claim") -> dict:
    if item_type not in ITEM_TYPES:
        raise ReviewError(f"Unknown item type '{item_type}'.")
    if decision not in DECISIONS:
        raise ReviewError("Decision must be 'confirmed' or 'dismissed'.")
    reason = normalize_ws(reason or "")
    if not reason:
        raise ReviewError("A reason is required.")
    item = (_latest_signal if item_type == "signal" else _latest_claim)(conn, company_id, fingerprint)
    if item is None:  # also refuses a fingerprint of another company
        raise ReviewError("No such item for this company.")
    status, signature = _state(item, item_type)
    snapshot = {"status": status, "evidence_signature": signature,
                "summary": item.get("quote") or item.get("title"), "key": item.get("key") or item.get("kind")}
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


def apply_reviews(conn: sqlite3.Connection, company_id: int, items: list[dict], item_type: str = "claim") -> list[dict]:
    """Add `review` and `review_state` to each claim or signal: confirmed, dismissed, or awaiting review."""
    decisions = latest_decisions(conn, company_id, item_type)
    for item in items:
        review = decisions.get(item["fingerprint"])
        item["review"], item["evidence_changed"] = None, False
        if review is None:
            item["review_state"] = "awaiting_review"
            continue
        seen = review.pop("seen")
        changed = (seen.get("status"), seen.get("evidence_signature")) != _state(item, item_type)
        item["review"] = review | {"status_then": seen.get("status")}
        item["evidence_changed"] = changed
        item["review_state"] = "awaiting_review" if changed else review["decision"]
    return items


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


def _latest_signal(conn: sqlite3.Connection, company_id: int, fingerprint: str) -> dict | None:
    """The signal with this fingerprint in the company's newest finished monitoring run."""
    return next((s for s in latest_monitor_results(conn, company_id)["signals"] if s["fingerprint"] == fingerprint), None)
