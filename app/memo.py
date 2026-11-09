"""Investment memo, built by the program from the checked results (no model call).

It follows the structure of a VC investment memo and only reorganizes what the checks found:
summary line, market, competition, verified findings (confirmed by the team, by category), risks
(regulatory checklist, alignment, risks the deck states), portfolio signals, items awaiting review, and
questions for the founder. Findings are only what an investor confirmed; everything else is labelled
as awaiting review. Nothing here is a new conclusion.
"""

import sqlite3
from datetime import datetime

from app.claims import latest_results
from app.monitor import latest_results as latest_monitor_results
from app.presentation import CATEGORIES, build_summary
from app.reviews import apply_reviews, review_counts

STATUS_LABELS = {
    "contradicted": "Contradicted",
    "possibly_overstated": "Possibly overstated",
    "externally_supported": "Externally supported",
    "consistent_with_company_data": "Consistent with company data",
    "unverified": "Unverified",
}
FLAGS = ("contradicted", "possibly_overstated", "unverified")


def build_memo(conn: sqlite3.Connection, company_id: int) -> dict:
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()["name"]
    results = latest_results(conn, company_id)
    order = list(STATUS_LABELS)  # flags first, as on the page
    claims = sorted(apply_reviews(conn, company_id, results["claims"]), key=lambda c: order.index(c["status"]))
    changes = apply_reviews(conn, company_id, results["changes"], "drift")
    signals = apply_reviews(conn, company_id, latest_monitor_results(conn, company_id)["signals"], "signal")
    generated = datetime.now().astimezone()  # the server's own time zone: the machine the team runs it on
    return {
        "company": company,
        "generated_at": generated.strftime("%Y-%m-%d %H:%M %Z"),
        "run": results["run"],
        "claims": claims,
        "findings": [c for c in claims if c["review_state"] == "confirmed"],
        "awaiting_review": [c for c in claims if c["review_state"] == "awaiting_review"],
        "dismissed": [c for c in claims if c["review_state"] == "dismissed"],
        "changes": changes,
        "signals": signals,
        "competitors": results["competitors"],
        "alignment": results["alignment"],
        "regulatory": results["regulatory"],
        "summary": build_summary(claims, changes, results["regulatory"]),
        "counts": review_counts(claims, changes),  # the same counts as the claim-check page
    }


def memo_to_markdown(memo: dict) -> str:
    run, claims = memo["run"], memo["claims"]
    live = [c for c in claims if c["review_state"] != "dismissed" and c["key"] != "other"]
    changes = [c for c in memo["changes"] if c["review_state"] != "dismissed"]
    lines = [f"# Investment memo: {memo['company']}", "",
             f"Generated {memo['generated_at']}. "
             + (f"Claim check of {run['filename']}." if run else "No claim check yet."),
             "This memo reorganizes the checked results; it adds no new conclusions. Evidence statuses describe the "
             "evidence found, not whether a claim is true.", ""]

    lines += ["## Summary", "", _summary_line(memo), ""]

    lines += ["## Market", ""]
    lines += [_claim_line(c) for c in live if c["category"] == "Market"] or ["No market claims checked."]
    market_changes = [c for c in changes if c["key"].startswith("market.")]
    if market_changes:
        lines += ["", "Changed since the last deck:"] + [_change_line(c) for c in market_changes]
    lines.append("")

    lines += ["## Competition", ""]
    lines += [_claim_line(c) for c in live if c["category"] == "Competition"] or ["No competition claims checked."]
    if memo["competitors"]:
        lines += ["", "Competitors found by web research (not reviewed):"]
        lines += [f"- {c['name']}" + (f": {c['description']}" if c["description"] else "")
                  + f" Source ({c['source_type'] or 'web'}): {c['url']}" for c in memo["competitors"]]
    lines.append("")

    lines += ["## Verified findings (confirmed by the investment team)", ""]
    confirmed_changes = [c for c in memo["changes"] if c["review_state"] == "confirmed"]
    if not memo["findings"] and not confirmed_changes:
        lines += ["No findings confirmed yet.", ""]
    for category in CATEGORIES:
        items = [c for c in memo["findings"] if c["category"] == category]
        moved = [c for c in confirmed_changes if _change_category(c) == category]
        if items or moved:
            lines += [f"### {category}", ""]
            for c in items:
                lines += _claim_detail(c) + ([f"  - Investor note: {c['review']['reason']}"] if c["review"]["reason"] else [])
            lines += [_change_line(c) for c in moved]
            lines.append("")

    lines += ["## Risks", ""]
    regulatory = memo["regulatory"] or {}
    if regulatory.get("industry"):
        lines += [f"### Regulatory ({regulatory['industry']['name']} checklist)", "",
                  "\"Not addressed in materials\" means the files do not mention it; it is not a finding of "
                  "non-compliance.", ""]
        for i in regulatory["items"]:
            where = f" \"{i['quote']}\" ({i['filename']})" if i["quote"] else ""
            lines.append(f"- **{i['name']}**: {i['status']}.{where} {i['why']}")
        lines.append("")
    alignment = memo["alignment"]
    if alignment and (alignment["points"] or alignment["pricing"]):
        lines += ["### Alignment (does the company earn more when its customers are better off?)", ""]
        for role, title in (("payer", "Who pays"), ("pricing", "How it charges"), ("beneficiary", "Who benefits")):
            lines += [f"- {title}: \"{r['quote']}\" ({_page(r, run)})" for r in alignment[role]]
        lines += [f"- **{p['title']}.** {p['note']}" for p in alignment["points"]]
        lines.append("")
    stated = [c for c in live if c["key"] == "risk.stated"]
    if stated:
        lines += ["### Risks the deck states", ""] + [f"- \"{c['quote']}\" ({c['loc_label']})" for c in stated] + [""]

    signals = memo["signals"]
    if signals:
        confirmed_signals = [s for s in signals if s["review_state"] == "confirmed"]
        waiting_signals = [s for s in signals if s["review_state"] == "awaiting_review"]
        lines += ["## Portfolio signals", "", "### Portfolio signals confirmed by the investment team", ""]
        lines += [f"- **{s['level'].title()}** ({s['period_end']}): {s['title']}. {s['detail']}"
                  + (f" Investor note: {s['review']['reason']}" if s["review"]["reason"] else "")
                  for s in confirmed_signals] or ["No signals confirmed yet."]
        lines += ["", f"### Portfolio signals awaiting review ({len(waiting_signals)})", "",
                  "Computed by rule from the updates. Not conclusions until an investor confirms them.", ""]
        for s in waiting_signals:
            changed = f" (evidence changed since it was {s['review']['decision']})" if s["evidence_changed"] else ""
            lines.append(f"- **{s['level'].title()}** ({s['period_end']}): {s['title']}. {s['detail']}{changed}")
        dismissed = sum(s["review_state"] == "dismissed" for s in signals)
        lines += (["", f"Dismissed signals: {dismissed}."] if dismissed else []) + [""]

    waiting = [c for c in memo["awaiting_review"] if c["status"] in FLAGS and c["key"] != "other"]
    waiting_changes = [c for c in changes if c["review_state"] == "awaiting_review"]
    lines += [f"## Awaiting review ({memo['counts']['awaiting_review']})", "",
              "Flagged by the system. Not conclusions until an investor confirms them.", ""]
    for category in CATEGORIES:
        items = [c for c in waiting if c["category"] == category]
        if items:
            lines += [f"### {category}", ""]
            for c in items:
                lines += _claim_detail(c)
                if c["evidence_changed"]:
                    lines.append(f"  - Evidence changed since it was {c['review']['decision']}.")
            lines.append("")
    if waiting_changes:
        lines += ["### Changed since the last deck", ""] + [_change_line(c) for c in waiting_changes] + [""]

    questions = founder_questions(memo)
    lines += ["## Questions for the founder", ""]
    lines += [f"{n}. {q}" for n, q in enumerate(questions, 1)] or ["No questions."]
    lines.append("")

    gone = [c for c in memo["dismissed"] if c["key"] != "other"] + \
        [c for c in memo["changes"] if c["review_state"] == "dismissed"]
    if gone:
        lines += [f"## Dismissed ({len(gone)})", ""]
        lines += [f"- {c['label']}: " + (f"\"{c['quote']}\"" if "quote" in c else c["note"])
                  + f" Reason: {c['review']['reason']}" for c in gone] + [""]

    if run and run["notes"]:
        lines += ["## Notes on this check", ""] + [f"- {n}" for n in run["notes"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


def founder_questions(memo: dict) -> list[str]:
    """Every question attached to an item that is not dismissed, in memo order."""
    claims = [c for c in memo["claims"] if c["review_state"] != "dismissed" and c.get("question")]
    out = []
    for category in CATEGORIES:
        out += [c["question"]["text"] for c in claims if c["category"] == category]
    out += [c["question"]["text"] for c in memo["changes"] if c["review_state"] != "dismissed" and c.get("question")]
    if memo["alignment"]:
        out += [p["question"]["text"] for p in memo["alignment"]["points"] if p.get("question")]
    out += [i["question"]["text"] for i in (memo["regulatory"] or {}).get("items", []) if i.get("question")]
    out += [s["question"]["text"] for s in memo["signals"] if s["review_state"] != "dismissed" and s.get("question")]
    return list(dict.fromkeys(out))  # two claims can share one question (both sides of a contradiction)


def _summary_line(memo: dict) -> str:
    claims, counts = [c for c in memo["claims"] if c["key"] != "other"], memo["counts"]

    def count(status: str) -> int:
        return sum(c["status"] == status for c in claims)

    supported = count("externally_supported") + count("consistent_with_company_data")
    top = memo["summary"]["top"]
    lead = f" Most important: {top[0]['text']}" if top else ""
    return (f"{counts['claims']} claims checked: {count('contradicted')} contradicted, {count('possibly_overstated')} "
            f"possibly overstated, {count('unverified')} unverified, {supported} supported; "
            f"{counts['confirmed']} confirmed by the team.{lead}")


def _change_category(change: dict) -> str:
    return "Market" if change["key"].startswith(("market.", "product.")) else \
        "Team" if change["key"].startswith("team.") else "Financials"


def _page(item: dict, run: dict | None) -> str:
    return (item.get("loc_label") or "").replace((run or {}).get("filename") or "", "").strip()


def _claim_line(c: dict) -> str:
    head = f" {c['evidence'][0]['headline']}." if c["evidence"] else " No evidence found."
    return f"- **{STATUS_LABELS[c['status']]}** · {c['label']}: \"{c['quote']}\" ({c['loc_label']}).{head}"


def _claim_detail(c: dict) -> list[str]:
    lines = [f"- **{STATUS_LABELS[c['status']]}** · {c['label']}: \"{c['quote']}\" ({c['loc_label']})"]
    for e in c["evidence"]:
        kind = f" ({e['source_type']})" if e.get("source_type") else ""
        source = f" Source{kind}: {e['url']}" if e.get("url") else ""
        lines.append(f"  - **{e['headline']}.** {e['note']}{source}")
    return lines


def _change_line(c: dict) -> str:
    head = f"**Changed** ({c['label']})" if c["kind"] == "changed" else f"Updated ({c['label']})"
    confirmed = ""
    if c["review_state"] == "confirmed":
        confirmed = " Confirmed" + (f": {c['review']['reason']}" if c["review"]["reason"] else ".")
    return f"- {head}: {c['note']}{confirmed}"
