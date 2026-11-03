"""Run the 7 required checks end to end and write docs/verification.md.

Usage:  python scripts/verify.py           # real models (needs ANTHROPIC_API_KEY)
        python scripts/verify.py --fake    # FakeLLM only (no network; model behaviour scripted)

Everything goes through the HTTP API (FastAPI TestClient, in-process) on a fresh
temporary database, exactly as the UI would call it: create companies, upload the
sample files, ask questions, generate a brief, restart the app.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "samples"
OUT = ROOT / "docs" / "verification.md"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="use FakeLLM instead of the real models")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="deal-room-verify-")
    os.environ["DB_PATH"] = str(Path(tmp) / "verify.db")

    from fastapi.testclient import TestClient

    from app.citations import quote_in_text
    from app.config import get_settings
    from app.db import connect
    from app.main import app, get_chat_llm, get_extract_llm

    settings = get_settings()
    if args.fake:
        from app.llm import FakeLLM
        from tests.helpers import KITEBRIDGE_RULES, TIDEWELL_RULES, fake_extractor
        app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(
            default=fake_extractor(TIDEWELL_RULES + KITEBRIDGE_RULES))
        mode = "FakeLLM (scripted extraction; Q&A and brief checks are skipped)"
    else:
        mode = f"real models: extraction `{settings.llm_extract_model}`, Q&A/brief `{settings.llm_chat_model}`"

    results: list[tuple[str, bool, list[str]]] = []
    transcript: list[str] = []

    def check(name: str, ok: bool, evidence: list[str]) -> None:
        results.append((name, ok, evidence))
        print(("PASS " if ok else "FAIL ") + name)

    def upload(client, cid, rel, doc_type, doc_date):
        path = SAMPLES / rel
        r = client.post(f"/api/companies/{cid}/documents", files={"file": (path.name, path.read_bytes())},
                        data={"doc_type": doc_type, "doc_date": doc_date})
        r.raise_for_status()
        body = r.json()
        transcript.append(f"- upload `{path.name}` → {body['chunks']} chunks, extraction: "
                          f"{json.dumps({k: v for k, v in body['extraction'].items() if k != 'dropped'})}")
        return body

    def ask(client, cid, question):
        t = time.time()
        r = client.post(f"/api/companies/{cid}/ask", json={"question": question})
        r.raise_for_status()
        a = r.json()
        transcript.append(f"\n**Q ({time.time() - t:.0f}s, tools: {', '.join(x['tool'] for x in a['tool_calls'])}):** {question}")
        for c in a["claims"]:
            transcript.append(f"- {c['text']}  \n  sources: " + "; ".join(
                f"`{x['loc_label']}` “{x['quote']}”" for x in c["citations"]))
        for o in a["open_items"]:
            transcript.append(f"- _open item:_ {o}")
        for c in a["conflicts"]:
            transcript.append(f"- _conflict attached:_ {c['metric']} {c['as_of_date']} values {c['values']}")
        return a

    def all_citations(answer):
        return [x for c in answer["claims"] for x in c["citations"]]

    def chunk_text(chunk_id):
        conn = connect(os.environ["DB_PATH"])
        try:
            return conn.execute("SELECT text FROM chunks WHERE id = ?", (chunk_id,)).fetchone()[0]
        finally:
            conn.close()

    with TestClient(app) as client:
        a = client.post("/api/companies", json={"name": "Tidewell Insurance"}).json()["id"]
        b = client.post("/api/companies", json={"name": "Kitebridge Finance"}).json()["id"]
        transcript.append("### Setup: initial files")
        upload(client, a, "company_a/tidewell_deck.pdf", "deck", "2026-07-15")
        upload(client, a, "company_a/tidewell_meeting_notes.md", "notes", "2026-07-22")
        upload(client, a, "company_a/tidewell_crm.csv", "crm", "2026-07-22")
        upload(client, b, "company_b/kitebridge_deck.pdf", "deck", "2026-04-10")
        upload(client, b, "company_b/kitebridge_meeting_notes.md", "notes", "2026-04-20")
        upload(client, b, "company_b/kitebridge_crm.csv", "crm", "2026-04-20")

        metrics = client.get(f"/api/companies/{a}/metrics").json()
        checks_before = client.get(f"/api/companies/{a}/checks").json()
        transcript.append("\nTidewell metrics: " + "; ".join(
            f"{m['metric']}={m['value_raw']} as of {m['as_of_date']} (`{m['loc_label']}`)" for m in metrics))

        # 1. registered != paying
        ev = [f"metric rows: {[(m['metric'], m['value']) for m in metrics]}"]
        ok1 = (("registered_customers", 100.0) in {(m["metric"], m["value"]) for m in metrics}
               and not any(m["metric"] == "paying_customers" and m["value"] == 100 for m in metrics))
        if not args.fake:
            q1 = ask(client, a, "How many paying customers does the company have?")
            claims = " ".join(c["text"] for c in q1["claims"])
            ok1 = ok1 and "60" in claims and not any(
                "100" in c["text"] and "paying" in c["text"].lower() for c in q1["claims"])
            ev.append(f"answer: {claims!r}")
        check("1. Registered customers are never reported as paying customers", ok1, ev)

        # 2. revenue missing
        ev = [f"checks.missing = {checks_before['missing']}"]
        ok2 = "revenue" in checks_before["missing"]
        if not args.fake:
            q2 = ask(client, a, "What is the company's revenue?")
            open_items = q2["open_items"]
            ok2 = ok2 and any("not found in the provided materials" in o.lower() for o in open_items) and not any(
                m["metric"] == "revenue" for m in metrics)
            ev.append(f"open items: {open_items}")
        check("2. Missing revenue is shown explicitly as missing", ok2, ev)

        # 3 + 6a. conflict after the 4th upload, both sources kept
        no_conflict_before = checks_before["conflicts"] == []
        transcript.append("\n### Upload the 4th file")
        upload(client, a, "company_a/tidewell_update_2026-08.txt", "notes", "2026-08-10")
        conflicts = client.get(f"/api/companies/{a}/checks").json()["conflicts"]
        paying = [c for c in conflicts if c["metric"] == "paying_customers"]
        ev = [f"before upload: conflicts = {checks_before['conflicts']}",
              "after upload: " + "; ".join(f"{s['value_raw']} @ {s['loc_label']}" for c in paying for s in c["sources"])]
        ok3 = no_conflict_before and len(paying) == 1 and paying[0]["values"] == [60, 80] and len(paying[0]["sources"]) == 2
        q3 = None
        if not args.fake:
            q3 = ask(client, a, "How many paying customers does the company have?")
            text = " ".join(c["text"] for c in q3["claims"])
            ok3 = ok3 and "60" in text and "80" in text and [c["values"] for c in q3["conflicts"]] == [[60, 80]]
            ev.append(f"answer after upload: {text!r}")
        check("3. A same-basis conflict keeps both sources", ok3, ev)

        # 4. citations support the answer
        ev, ok4 = [], True
        if not args.fake:
            answers = [q1, q2, q3]
            for ans in answers:
                for cit in all_citations(ans):
                    verbatim = quote_in_text(cit["quote"], chunk_text(cit["chunk_id"]))
                    ok4 &= verbatim
            q1_quotes = [c["quote"] for c in all_citations(q1)]
            ok4 &= any("60 paying customers" in q for q in q1_quotes)
            q3_quotes = " ".join(c["quote"] for c in all_citations(q3))
            ok4 &= "60" in q3_quotes and "80" in q3_quotes
            ev.append(f"{sum(len(all_citations(x)) for x in answers)} citations re-checked verbatim against the DB")
            ev.append(f"paying-customers answer cites: {q1_quotes}")
        metric_quotes_ok = all(quote_in_text(m["quote"], chunk_text(m["chunk_id"])) for m in metrics)
        ok4 &= metric_quotes_ok
        ev.append(f"all {len(metrics)} metric quotes verbatim in their chunks: {metric_quotes_ok}")
        check("4. Citations point to source text that supports the answer", ok4, ev)

        # 5. company isolation
        ev, ok5 = [], True
        for q in ["paying customers", "Kitebridge credit lines", "total funding", "Lee Park"]:
            res = client.get(f"/api/companies/{a}/search", params={"q": q, "k": 50}).json()["results"]
            ok5 &= all(r["filename"].startswith("tidewell") for r in res)
        ev.append("search over 4 queries (k=50) for Tidewell: only tidewell_* files")
        a_files = {m["filename"] for m in client.get(f"/api/companies/{a}/metrics").json()}
        ok5 &= all(f.startswith("tidewell") for f in a_files)
        b_conflicts = client.get(f"/api/companies/{b}/checks").json()["conflicts"]
        ok5 &= b_conflicts == []
        ev.append(f"Kitebridge conflicts after Tidewell's 4th upload: {b_conflicts}")
        if not args.fake:
            q5 = ask(client, a, "How much funding has Kitebridge Finance raised, and how many paying customers does it have?")
            files = {c["filename"] for c in all_citations(q5)}
            # Must neither leak Kitebridge data nor answer with Tidewell's numbers as if they were Kitebridge's.
            ok5 &= q5["claims"] == [] and bool(q5["open_items"])
            ev.append(f"asking Tidewell about Kitebridge → claims {len(q5['claims'])}, cited files {sorted(files)}, "
                      f"open items {q5['open_items']}")
        check("5. Other companies' materials never enter retrieval, answers or briefs", ok5, ev)

        # 7. brief: every entry sourced (checked before restart, also for isolation)
        if not args.fake:
            t = time.time()
            brief = client.post(f"/api/companies/{a}/brief", json={"requirements": "focus on traction and risks"}).json()
            entries = ([x for s in brief["sections"] for i in s["items"] for x in i["citations"]]
                       + brief["key_metrics"] + brief["recent_communications"] + brief["unclear_basis"]
                       + [s for c in brief["conflicts"] for s in c["sources"]])
            sourced = all(e.get("loc_label") and e.get("quote") and quote_in_text(e["quote"], chunk_text(e["chunk_id"]))
                          for e in entries)
            bullets_ok = all(i["citations"] for s in brief["sections"] for i in s["items"])
            no_b = all(not e["loc_label"].startswith("kitebridge") for e in entries)
            ok7 = bool(brief["sections"]) and sourced and bullets_ok and no_b and \
                [m["metric"] for m in brief["missing"]] == ["revenue"]
            ev7 = [f"{len(entries)} sourced entries, all quotes verbatim: {sourced}",
                   f"sections: {[s['title'] for s in brief['sections']]}, missing: {[m['metric'] for m in brief['missing']]}",
                   f"no Kitebridge source in the Tidewell brief: {no_b}"]
            transcript.append(f"\n### Brief ({time.time() - t:.0f}s), requirements: focus on traction and risks\n")
            transcript.append("```markdown\n" + brief["markdown"] + "```")

    # 6. new upload changes answers (above) + data survives a restart
    with TestClient(app) as client:  # new app startup on the same database file
        companies = client.get("/api/companies").json()
        docs = client.get(f"/api/companies/{a}/documents").json()
        conflicts_after_restart = client.get(f"/api/companies/{a}/checks").json()["conflicts"]
        ok6 = (no_conflict_before and bool(paying) and len(docs) == 4
               and [c["values"] for c in conflicts_after_restart] == [[60, 80]])
        ev6 = [f"conflict absent before the 4th upload and present after it: {no_conflict_before and bool(paying)}",
               f"after restart: {len(companies)} companies, {len(docs)} Tidewell documents, "
               f"conflicts {[c['values'] for c in conflicts_after_restart]}"]
        if not args.fake:
            q6 = ask(client, a, "When was our last conversation with the company, and what are the next steps?")
            labels = {x["loc_label"] for x in all_citations(q6)}
            ok6 = ok6 and "tidewell_crm.csv row 3" in labels
            ev6.append(f"question after restart cites {sorted(labels)}")
        check("6. New uploads change later answers; saved data is read back after a restart", ok6, ev6)

    if not args.fake:
        check("7. Every brief entry has a source", ok7, ev7)
    else:
        results.append(("7. Every brief entry has a source", None, ["not run in --fake mode (see tests/test_brief.py)"]))

    write_report(mode, results, transcript)
    print(f"Wrote {OUT}")


def write_report(mode, results, transcript):
    lines = [
        "# Verification of the 7 required checks", "",
        f"Generated by `python scripts/verify.py` on {time.strftime('%Y-%m-%d %H:%M')}.", "",
        f"**Mode:** {mode}.", "",
        "The script uses the HTTP API on a fresh temporary database: create companies, upload the",
        "SYNTHETIC SAMPLE files, ask questions, upload the 4th file, generate a brief, restart the app.",
        "Every number and citation below comes from that run; nothing is hard-coded.", "",
        "| Check | Result |", "|---|---|",
    ]
    for name, ok, _ in results:
        lines.append(f"| {name} | {'PASS' if ok else ('not run' if ok is None else 'FAIL')} |")
    lines += ["", "## Evidence", ""]
    for name, ok, evidence in results:
        lines.append(f"### {name}: {'PASS' if ok else ('not run' if ok is None else 'FAIL')}")
        lines += [f"- {e}" for e in evidence] + [""]
    lines += ["## Transcript of the run", ""] + transcript + [""]
    lines += [
        "## Deterministic tests (FakeLLM, no network)", "",
        "The same behaviours are also covered by offline tests that run on every `pytest`:",
        "- 1, 3: `tests/test_rules.py` (registered vs paying, 60 vs 80 conflict with both sources)",
        "- 2: `tests/test_rules.py::test_revenue_is_listed_as_missing`, `tests/test_agent.py::test_not_found_open_item_passes_through`",
        "- 4: `tests/test_extract.py`, `tests/test_agent.py` (fabricated quotes dropped), `tests/test_numbers_citations.py`",
        "- 5: `tests/test_rag.py::test_search_never_returns_other_company_chunks`, `tests/test_agent.py::test_company_id_from_model_is_ignored`",
        "- 6: `tests/test_api_documents.py::test_data_survives_server_restart`, `tests/test_rag.py::test_replaced_file_chunks_never_returned`",
        "- 7: `tests/test_brief.py::test_every_brief_entry_has_a_source`", "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
