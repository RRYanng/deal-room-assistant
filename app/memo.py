"""Investment memo, built by the program from the checked results (no model call).

It holds only what the investment team confirmed: a one-line summary, verified findings by category,
confirmed portfolio signals and the questions for the founder those items raise. Items nobody has reviewed
are not listed; the memo ends with how many still await review on the Deck check page. Nothing here is a
new conclusion.
"""

import sqlite3
from datetime import datetime

from app.claims import latest_results
from app.monitor import latest_results as latest_monitor_results
from app.presentation import CATEGORIES, build_summary, claim_rows, hide_parts, visible_summary
from app.reviews import apply_reviews, review_counts

STATUS_LABELS = {
    "contradicted": "Contradicted",
    "possibly_overstated": "Possibly overstated",
    "externally_supported": "Externally supported",
    "consistent_with_company_data": "Consistent with company data",
    "unverified": "Unverified",
}


def build_memo(conn: sqlite3.Connection, company_id: int, hidden: frozenset[str] = frozenset()) -> dict:
    """`hidden` parts ("changes", "regulatory") are left out, with their sections and questions."""
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()["name"]
    results = hide_parts(latest_results(conn, company_id), hidden)
    order = list(STATUS_LABELS)  # flags first, as on the page
    claims = sorted(apply_reviews(conn, company_id, results["claims"]), key=lambda c: order.index(c["status"]))
    rows = claim_rows(claims)  # as on the page: the two sides of a contradiction are one item
    changes = apply_reviews(conn, company_id, results["changes"], "drift")
    signals = apply_reviews(conn, company_id, latest_monitor_results(conn, company_id)["signals"], "signal")
    generated = datetime.now().astimezone()  # the server's own time zone: the machine the team runs it on
    return {
        "company": company,
        "generated_at": generated.strftime("%Y-%m-%d %H:%M %Z"),
        "run": results["run"],
        "claims": rows,
        "findings": [c for c in rows if c["review_state"] == "confirmed"],
        "awaiting_review": [c for c in rows if c["review_state"] == "awaiting_review"],
        "dismissed": [c for c in rows if c["review_state"] == "dismissed"],
        "changes": changes,
        "signals": signals,
        "competitors": results["competitors"],
        "alignment": results["alignment"],
        "regulatory": results["regulatory"],
        "summary": visible_summary(build_summary(claims, changes, results["regulatory"]), hidden),
        "counts": review_counts(claims, changes),  # the same counts as the claim-check page
    }


NOTHING_CONFIRMED = "Nothing confirmed yet. Confirm findings on the Deck check page to add them here."


def memo_to_markdown(memo: dict) -> str:
    run = memo["run"]
    findings = memo["findings"]
    changes = [c for c in memo["changes"] if c["review_state"] == "confirmed"]
    signals = [s for s in memo["signals"] if s["review_state"] == "confirmed"]
    lines = [f"# Investment memo: {memo['company']}", "",
             f"Generated {memo['generated_at']}. "
             + (f"Deck check of {run['filename']}." if run else "No deck check yet."), ""]

    if not findings and not changes and not signals:
        lines += [NOTHING_CONFIRMED, ""]
    else:
        lines[-1:] = ["Only what the investment team confirmed. Evidence statuses describe the evidence found, not "
                      "whether a claim is true.", ""]
        lines += ["## Summary", "", _summary_line(findings, changes, signals), ""]

        if findings or changes:
            lines += ["## Verified findings (confirmed by the investment team)", ""]
            for category in CATEGORIES:
                items = [c for c in findings if c["category"] == category]
                moved = [c for c in changes if _change_category(c) == category]
                if items or moved:
                    lines += [f"### {category}", ""]
                    for c in items:
                        lines += _claim_detail(c)
                        if c["review"]["reason"]:
                            lines.append(f"  - Investor note: {c['review']['reason']}")
                    lines += [_change_line(c) for c in moved]
                    lines.append("")

        if signals:
            lines += ["## Portfolio signals (confirmed by the investment team)", ""]
            lines += [f"- **{s['level'].title()}** ({s['period_end']}): {s['title']}. {s['detail']}"
                      + (f" Investor note: {s['review']['reason']}" if s["review"]["reason"] else "")
                      for s in signals]
            lines.append("")

        questions = founder_questions(memo)
        if questions:
            lines += ["## Questions for the founder", ""] + [f"{n}. {q}" for n, q in enumerate(questions, 1)] + [""]

    waiting = memo["counts"]["awaiting_review"]  # the count the Deck check page shows
    if waiting:
        lines.append(f"{waiting} {'item' if waiting == 1 else 'items'} still awaiting review on the Deck check page.")
    return "\n".join(lines).rstrip() + "\n"


def founder_questions(memo: dict) -> list[str]:
    """The questions of confirmed items only, in memo order."""
    claims = [m for c in memo["findings"] for m in c.get("members", [c]) if m.get("question")]
    out = []
    for category in CATEGORIES:
        out += [c["question"]["text"] for c in claims if c["category"] == category]
    out += [c["question"]["text"] for c in memo["changes"] if c["review_state"] == "confirmed" and c.get("question")]
    out += [s["question"]["text"] for s in memo["signals"] if s["review_state"] == "confirmed" and s.get("question")]
    return list(dict.fromkeys(out))  # two claims can share one question (both sides of a contradiction)


def _summary_line(findings: list[dict], changes: list[dict], signals: list[dict]) -> str:
    """"2 findings confirmed by the investment team (1 Market, 1 Financials): 1 contradicted, 1 possibly
    overstated." Portfolio signals follow in their own sentence."""
    signal_text = f"{len(signals)} portfolio signal{'' if len(signals) == 1 else 's'} confirmed."
    total = len(findings) + len(changes)
    if not total:
        return f"No deck findings confirmed yet; {signal_text[:-1]}."
    categories = [c["category"] for c in findings] + [_change_category(c) for c in changes]
    by_category = ", ".join(f"{categories.count(cat)} {cat}" for cat in CATEGORIES if cat in categories)
    statuses = [STATUS_LABELS[c["status"]].lower() for c in findings]
    by_status = [f"{statuses.count(label)} {label}" for label in dict.fromkeys(statuses)]
    if changes:
        by_status.append(f"{len(changes)} changed since the last deck")
    line = (f"{total} finding{'' if total == 1 else 's'} confirmed by the investment team ({by_category}): "
            f"{', '.join(by_status)}.")
    return line + (f" {signal_text}" if signals else "")


def _change_category(change: dict) -> str:
    return "Market" if change["key"].startswith(("market.", "product.")) else \
        "Team" if change["key"].startswith("team.") else "Financials"


def _claim_detail(c: dict) -> list[str]:
    members = c.get("members")
    if not members:
        lines = [f"- **{STATUS_LABELS[c['status']]}** · {c['label']}: \"{c['quote']}\" ({c['loc_label']})"]
    else:  # one item: the conclusion, then each quote with its own evidence, as on the page
        lines = [f"- **{STATUS_LABELS[c['status']]}** · {c['label']}: {c['headline']}"]
    evidence = [[_evidence_line(e) for e in m["evidence"]] for m in members or [c]]
    for i, m in enumerate(members or [c]):
        if members:
            lines.append(f"  - \"{m['quote']}\" ({m['loc_label']})")
        if i + 1 == len(evidence) or evidence[i] != evidence[i + 1]:  # evidence shared with the next claim comes once, after it
            lines += evidence[i]
    return lines


def _evidence_line(e: dict) -> str:
    kind = f" ({e['source_type']})" if e.get("source_type") else ""
    source = f" Source{kind}: {e['url']}" if e.get("url") else ""
    return f"  - **{e['headline']}.** {e['note']}{source}"


def _change_line(c: dict) -> str:
    head = f"**Changed** ({c['label']})" if c["kind"] == "changed" else f"Updated ({c['label']})"
    confirmed = ""
    if c["review_state"] == "confirmed":
        confirmed = " Confirmed" + (f": {c['review']['reason']}" if c["review"]["reason"] else ".")
    return f"- {head}: {c['note']}{confirmed}"
