"""Editing the memo: the investor's note under each confirmed item (edit and save), Remove (the confirmation is
withdrawn by a new review record, so the item is back under awaiting review), and the company's Investor notes
at the top (a new table, latest text wins), which the exports include and leave out when empty."""

import pytest
from fastapi.testclient import TestClient

from app.claims import create_run, run_claim_check
from app.config import PROJECT_ROOT
from app.db import SCHEMA, connect, init_db
from app.main import app, list_claims, memo
from app.memo import build_memo, memo_to_markdown, save_memo_notes
from app.monitor import create_run as create_monitor_run, run_monitor
from app.reviews import record_review
from tests.helpers_v2 import (DECK, by_quote, fake_web, load_clearclaim, monitor_llm, research_llm, review_row)
from tests.test_ui_dismissed import NODE, render

NOTE = "Founder said 70% today."


@pytest.fixture
def company(db):
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    run_claim_check(db, create_run(db, company_id, docs[DECK]), research_llm(), fake_web())
    run_monitor(db, create_monitor_run(db, company_id), monitor_llm())
    claims = list_claims(company_id, conn=db)["claims"]
    size = next(c for c in claims if c["key"] == "market.size_usd")
    record_review(db, company_id, size["fingerprint"], "confirmed", "")
    review_row(db, company_id, claims, "Gross margin is 67-82%", "confirmed", NOTE)
    return company_id


def reviews(db) -> int:
    return db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]


def test_memo_lists_its_confirmed_items_in_order_with_their_notes(db, company):
    built = build_memo(db, company)
    items = built["items"]
    assert [(i["item_type"], i["label"], i["note"]) for i in items] == [
        ("claim", "Market size", None), ("claim", "Gross margin", NOTE)]
    margin = by_quote(built["claims"], "Gross margin is 67-82%")
    assert items[1]["fingerprints"] == [m["fingerprint"] for m in margin["members"]]
    # the same order as the items of the memo's Verified findings
    findings = memo_to_markdown(built).split("## Verified findings")[1].split("\n## ")[0]
    assert [l.split(" · ")[1].split(":")[0] for l in findings.splitlines() if l.startswith("- ")] == \
        ["Market size", "Gross margin"]


def test_editing_a_note_adds_a_review_and_the_memo_shows_the_new_text(db, company):
    before = reviews(db)
    margin = build_memo(db, company)["items"][1]
    for fp in margin["fingerprints"]:
        record_review(db, company, fp, "confirmed", "Asked for the payer mix.")
    assert reviews(db) == before + 2  # appended, nothing replaced
    text = memo_to_markdown(build_memo(db, company))
    assert "Investor note: Asked for the payer mix." in text and NOTE not in text


def test_remove_withdraws_the_confirmation_and_the_item_awaits_review_again(db, company):
    page = list_claims(company, conn=db)
    waiting, before = page["counts"]["awaiting_review"], reviews(db)
    margin = build_memo(db, company)["items"][1]
    for fp in margin["fingerprints"]:
        record_review(db, company, fp, "reopened", "")
    assert reviews(db) == before + 2
    assert db.execute("SELECT COUNT(*) FROM reviews WHERE decision = 'confirmed'").fetchone()[0] == 3  # history kept
    page = list_claims(company, conn=db)
    row = next(r for r in page["rows"] if r["label"] == "Gross margin")
    assert row["review_state"] == "awaiting_review" and row["review"] is None  # as if never decided
    assert page["counts"]["awaiting_review"] == waiting + 1 and page["counts"]["confirmed"] == 1
    built = build_memo(db, company)
    assert [i["label"] for i in built["items"]] == ["Market size"]
    assert "Gross margin" not in memo_to_markdown(built)


def test_notes_of_changes_and_signals_are_their_own_line(db, company):
    built = build_memo(db, company)
    slip = next(s for s in built["signals"] if s["kind"] == "milestone_slip")
    record_review(db, company, slip["fingerprint"], "confirmed", "New launch plan requested.", item_type="signal")
    change = next(c for c in built["changes"] if c["key"] == "market.customer_count")
    record_review(db, company, change["fingerprint"], "confirmed", "Definition changed.", item_type="drift")
    text = memo_to_markdown(build_memo(db, company))
    assert "\n  - Investor note: New launch plan requested." in text
    assert "\n  - Investor note: Definition changed." in text
    assert [i["item_type"] for i in build_memo(db, company)["items"]] == ["claim", "drift", "claim", "signal"]


def test_reviews_table_accepts_reopened_and_keeps_old_rows(tmp_path):
    path = tmp_path / "before.db"
    conn = connect(path)
    old = SCHEMA.replace("CHECK (decision IN ('confirmed', 'dismissed', 'reopened'))",
                         "CHECK (decision IN ('confirmed', 'dismissed'))").replace(
        "CHECK (decision IN ('confirmed', 'reopened') OR length(trim(coalesce(reason, ''))) > 0)",
        "CHECK (decision = 'confirmed' OR length(trim(coalesce(reason, ''))) > 0)")
    assert old != SCHEMA
    conn.executescript(old)
    cid = conn.execute("INSERT INTO companies(name) VALUES ('Acme')").lastrowid
    conn.execute("INSERT INTO reviews(company_id, item_type, fingerprint, decision, reason) VALUES (?, 'claim', 'fp',"
                 " 'dismissed', 'old reason')", (cid,))
    conn.commit()
    init_db(conn)
    assert [tuple(r) for r in conn.execute("SELECT fingerprint, decision, reason FROM reviews")] == \
        [("fp", "dismissed", "old reason")]
    conn.execute("INSERT INTO reviews(company_id, item_type, fingerprint, decision) VALUES (?, 'claim', 'fp', 'reopened')",
                 (cid,))
    init_db(conn)  # running it again changes nothing
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2


# --- the company's Investor notes ------------------------------------------------------------

def test_investor_notes_come_first_and_are_left_out_when_empty(db, company):
    assert "Investor notes" not in memo_to_markdown(build_memo(db, company))
    save_memo_notes(db, company, "Strong team.\n\nMargin story needs work before IC.")
    built = build_memo(db, company)
    assert built["notes"]["text"] == "Strong team.\n\nMargin story needs work before IC."
    text = memo_to_markdown(built)
    assert "## Investor notes\n\nStrong team.\n\nMargin story needs work before IC.\n\n## Summary" in text
    save_memo_notes(db, company, "   ")  # cleared: the section goes, the old text stays in the table
    assert "Investor notes" not in memo_to_markdown(build_memo(db, company))
    assert db.execute("SELECT COUNT(*) FROM memo_notes").fetchone()[0] == 2


def test_investor_notes_are_kept_per_company(db, company):
    other = db.execute("INSERT INTO companies(name) VALUES ('Kitebridge Finance')").lastrowid
    save_memo_notes(db, company, "ClearClaim view.")
    assert build_memo(db, other)["notes"]["text"] == ""
    assert "ClearClaim view." not in memo_to_markdown(build_memo(db, other))


def test_investor_notes_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    with TestClient(app) as client:
        cid = client.post("/api/companies", json={"name": "Acme"}).json()["id"]
        saved = client.put(f"/api/companies/{cid}/memo-notes", json={"text": "Pass for now."})
        assert saved.status_code == 200 and saved.json()["text"] == "Pass for now."
        assert client.put("/api/companies/999/memo-notes", json={"text": "x"}).status_code == 404
    with TestClient(app) as client:  # a new app start reads the same file
        body = client.get(f"/api/companies/{cid}/memo").json()
        assert body["notes"]["text"] == "Pass for now." and "## Investor notes\n\nPass for now." in body["markdown"]


# --- page (static/app.js in Node) ------------------------------------------------------------

def memo_page(db, company_id, code="renderMemo(); $(\"claims-body\").innerHTML"):
    built = memo(company_id, conn=db)
    return render(f"""claims.view = "memo"; claims.companyId = {company_id};
        claims.memo = {{...DATA, text: memoText(DATA.markdown)}}; {code}""", built)


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_each_confirmed_item_has_its_note_and_remove(db, company):
    from tests.test_ui_memo_fold import screen  # opened (items are folded to one line by default), on screen
    open_all = 'claims.memo.items.forEach((i) => claims.memoOpen.add(i.fingerprints.join(" ")));'
    html = screen(memo_page(db, company, open_all + ' renderMemo(); $("claims-body").innerHTML'))
    assert html.count('data-memo-remove="') == 2 and ">Remove<" in html
    assert 'data-note-edit="0"' in html and ">Add a note<" in html  # Market size has no note
    assert f">Investor note: {NOTE}<" in html.replace("\n", "")
    assert html.count(f"Investor note: {NOTE}") == 1  # the Markdown line is not shown a second time
    editing = memo_page(db, company, open_all + ' claims.memo.editingNote = 1; renderMemo(); $("claims-body").innerHTML')
    assert 'data-note-form="1"' in editing and f">{NOTE}</textarea>" in editing and ">Save<" in editing


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_investor_notes_box_on_top_and_the_section_printed(db, company):
    save_memo_notes(db, company, "Strong team.")
    html = memo_page(db, company)
    box = html.index('id="memo-notes"')
    assert box < html.index('class="memo-doc"') and ">Strong team.</textarea>" in html
    assert 'class="print-only"><h2>Investor notes</h2>' in html  # the box is not printed; the section is


def test_print_shows_notes_but_not_the_buttons():
    style = (PROJECT_ROOT / "static" / "style.css").read_text()
    printing = style[style.index("@media print"):]
    assert ("body.memo-open .memo-remove, body.memo-open .memo-note-add, body.memo-open .memo-notes-editor"
            " { display: none !important; }") in printing
    assert ".print-only { display: none; }" in style and "body.memo-open .print-only { display: block; }" in printing


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_library_no_longer_shows_the_chunk_count(db, company):
    from tests.test_library import library_page
    html = library_page(db, "libraryListHtml()")
    assert "chunk" not in html and "re-extract" in html


REMOVE_TEXT = "Remove this finding from the memo? It will go back to awaiting review on the Deck check page."


def test_the_page_has_an_in_page_remove_dialog():
    page = (PROJECT_ROOT / "static" / "index.html").read_text()
    dialog = page[page.index('<dialog id="remove-dialog"'):].split("</dialog>")[0]
    assert REMOVE_TEXT in dialog
    assert '<button type="button" id="remove-cancel" class="secondary">Cancel</button>' in dialog
    assert '<button type="button" id="remove-confirm" class="danger-button">Remove</button>' in dialog
    assert "confirm(" not in (PROJECT_ROOT / "static" / "app.js").read_text().replace("Confirm(", "")


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_remove_asks_first_and_cancel_changes_nothing(db, company):
    built = memo(company, conn=db)
    out = render(f"""(async () => {{
        const posts = [];
        fetch = (url, options) => {{ posts.push([url, options && options.body]); return new Promise(() => {{}}); }};
        const settle = async () => {{ for (let i = 0; i < 10; i++) await null; }};
        claims.view = "memo"; claims.companyId = {company}; claims.memo = {{...DATA, text: memoText(DATA.markdown)}};
        const cancelled = removeMemoItem(1);
        await settle();
        const asked = posts.length;
        finishRemove(false);
        await cancelled;
        await settle();
        const afterCancel = {{posts: posts.length, items: claims.memo.items.length}};
        removeMemoItem(1);
        await settle();
        finishRemove(true);
        await settle();
        return {{asked, afterCancel, posts}};
    }})()""", built)
    assert out["asked"] == 0  # nothing is sent while the dialog is open
    assert out["afterCancel"] == {"posts": 0, "items": 2}
    fingerprints = built["items"][1]["fingerprints"]
    assert out["posts"] and out["posts"][0][0] == f"/api/companies/{company}/reviews"
    assert '"decision":"reopened"' in out["posts"][0][1] and fingerprints[0] in out["posts"][0][1]
