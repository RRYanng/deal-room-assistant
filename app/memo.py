"""Investment memo from the claim check, built by the program (no model call).

Findings are only what an investor confirmed, each with the investor's reason. Everything flagged but
not reviewed is listed separately under "Awaiting review" and labelled as not a conclusion. Dismissed
items are counted, with their reasons, and never presented as findings.
"""

import sqlite3
from datetime import datetime, timezone

from app.claims import latest_results
from app.reviews import apply_reviews

STATUS_LABELS = {
    "contradicted": "Contradicted",
    "possibly_overstated": "Possibly overstated",
    "externally_supported": "Externally supported",
    "consistent_with_company_data": "Consistent with company data",
    "unverified": "Unverified",
}


def build_memo(conn: sqlite3.Connection, company_id: int) -> dict:
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()["name"]
    results = latest_results(conn, company_id)
    order = list(STATUS_LABELS)  # flags first, as on the page
    claims = sorted(apply_reviews(conn, company_id, results["claims"]), key=lambda c: order.index(c["status"]))
    return {
        "company": company,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "run": results["run"],
        "findings": [c for c in claims if c["review_state"] == "confirmed"],
        "awaiting_review": [c for c in claims if c["review_state"] == "awaiting_review"],
        "dismissed": [c for c in claims if c["review_state"] == "dismissed"],
        "competitors": results["competitors"],
    }


def memo_to_markdown(memo: dict) -> str:
    run = memo["run"]
    lines = [f"# Investment memo: {memo['company']}", "",
             f"Generated {memo['generated_at']}. "
             + (f"Claim check of {run['filename']}, run {run['id']} ({run['status']})." if run else "No claim check yet."),
             "Evidence statuses describe the evidence found, not whether a claim is true.", ""]

    lines += ["## Findings (confirmed by the investment team)", ""]
    if not memo["findings"]:
        lines += ["No findings confirmed yet.", ""]
    for c in memo["findings"]:
        lines += _claim_lines(c) + [f"  - Investor note: {c['review']['reason']}", ""]

    lines += [f"## Awaiting review ({len(memo['awaiting_review'])})", "",
              "Flagged by the system. Not conclusions until an investor confirms them.", ""]
    for c in memo["awaiting_review"]:
        lines += _claim_lines(c)
        if c["evidence_changed"]:
            lines.append(f"  - Evidence changed since it was {c['review']['decision']}.")
        lines.append("")

    if memo["dismissed"]:
        lines += [f"## Dismissed ({len(memo['dismissed'])})", ""]
        lines += [f"- {c['statement']} Reason: {c['review']['reason']}" for c in memo["dismissed"]] + [""]

    if memo["competitors"]:
        lines += ["## Competitors found by web research (not reviewed)", ""]
        lines += [f"- {c['name']}: {c['description'] or ''} Source: {c['url']}".replace(":  Source", ": Source")
                  for c in memo["competitors"]] + [""]

    if run and run["notes"]:
        lines += ["## Notes on this check", ""] + [f"- {n}" for n in run["notes"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


def _claim_lines(c: dict) -> list[str]:
    lines = [f"- **{STATUS_LABELS[c['status']]}**: \"{c['quote']}\" ({c['loc_label']})"]
    for e in c["evidence"]:
        kind = f" ({e['source_type']})" if e.get("source_type") else ""
        source = f" Source{kind}: {e['url']}" if e.get("url") else ""
        lines.append(f"  - {e['note']}{source}")
    return lines
