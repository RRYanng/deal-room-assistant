"""Library: a one-line summary under each file, built from stored data only, and a search box that filters
the list as you type and, on Enter, searches file contents with the BM25 keyword index. No model is called."""

import pytest

from app import rag
from app.config import PROJECT_ROOT
from app.ingest import ingest_document
from app.library import SUMMARY_MAX, document_summaries, search_files
from app.main import library_search, list_documents
from tests.helpers_v2 import DECK, SAMPLES, load_clearclaim
from tests.test_ui_dismissed import NODE, render

# What extraction stored for the December deck (as in data/demo_v2.db): (metric, label, value_raw, quote fragment)
DECK_METRICS = [
    ("other", "US dental care spending per year", "$400 billion", "Americans spend $400 billion"),
    ("other", "Independent dental practices in US", "400,000", "There are 400,000 independent"),
    ("other", "Price per practice per year", "$3,600", "At $3,600 per practice"),
    ("other", "Total addressable market", "$12B", "At $3,600 per practice"),
    ("paying_customers", None, "85", "85 dental practices pay"),
    ("other", "Gross margin", "67-82%", "Gross margin is 67-82%"),
    ("other", "Full-time employees", "14", "We are a team of 14"),
    ("other", "Annual operating costs 2025", "$420,000", "Total annual operating costs"),
    ("other", "Current gross margin", "above 90%", "Current gross margin: above 90%."),
]


def store_metrics(conn, company_id, document_id, metrics):
    for metric, label, value_raw, fragment in metrics:
        chunk = conn.execute("SELECT id, text FROM chunks WHERE document_id = ? AND text LIKE ? ORDER BY chunk_index",
                             (document_id, f"%{fragment}%")).fetchone()
        assert chunk, fragment
        conn.execute("INSERT INTO metrics(company_id, document_id, chunk_id, metric, metric_label, value_raw, quote)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (company_id, document_id, chunk["id"], metric, label, value_raw,
                                                      fragment))
    conn.commit()


@pytest.fixture
def library(db):
    company_id, docs = load_clearclaim(db, stage=("diligence", "portfolio"))
    store_metrics(db, company_id, docs[DECK], DECK_METRICS)
    kitebridge = db.execute("INSERT INTO companies(name) VALUES ('Kitebridge Finance')").lastrowid
    for name, doc_type in [("kitebridge_deck.pdf", "deck"), ("kitebridge_crm.csv", "crm")]:
        ingest_document(db, kitebridge, name, doc_type, "2026-04-20", (SAMPLES / "company_b" / name).read_bytes())
    db.commit()
    return company_id, docs


def test_summary_gives_type_date_and_key_metrics_first(db, library):
    company_id, docs = library
    summary = document_summaries(db, company_id)[docs[DECK]]
    assert summary.startswith("Pitch deck, Dec 1, 2025: 85 paying customers, $12B total addressable market, "
                              "14 full-time employees, 67-82% gross margin")


def test_long_summary_is_cut_to_one_line(db, library):
    company_id, docs = library
    summary = document_summaries(db, company_id)[docs[DECK]]
    assert len(summary) <= SUMMARY_MAX and summary.endswith(", …")


def test_file_without_metrics_uses_its_first_meaningful_sentence(db, library):
    company_id, docs = library
    summaries = document_summaries(db, company_id)
    # CRM rows: the SYNTHETIC SAMPLE note, the date and contact fields and "Intro call." are skipped
    assert summaries[docs["clearclaim_crm.csv"]] == (
        "CRM records, Dec 8, 2025: ClearClaim automates insurance verification and claims for dental practices.")
    update = summaries[docs["clearclaim_update_2026-Q1.txt"]]
    assert update.startswith("Notes, Apr 10, 2026: Subject: ClearClaim AI investor update")
    assert "SYNTHETIC" not in "".join(summaries.values())


def test_documents_api_returns_the_summary(db, library):
    company_id, docs = library
    files = {d["filename"]: d for d in list_documents(company_id, conn=db)}
    assert files[DECK]["summary"].startswith("Pitch deck, Dec 1, 2025: 85 paying customers")


def no_model(*_args, **_kwargs):
    raise AssertionError("the file search must not load the embedding model")


def test_enter_searches_file_contents_with_bm25_and_counts_mentions(db, library, monkeypatch):
    monkeypatch.setattr(rag, "get_embedder", no_model)
    company_id, docs = library
    found = search_files(db, "gross margin")
    assert [(f["filename"], f["mentions"]) for f in found] == [(DECK, 2), ("clearclaim_meeting_notes.md", 1)]
    assert found[0]["company"] == "ClearClaim AI" and found[0]["company_id"] == company_id
    assert found[0]["locations"] == ["p.5 ¶2", "p.9 ¶3"]
    assert library_search(q="Gross margins", conn=db)["results"] == found  # case and plurals fold like the index
    assert search_files(db, "the of") == [] and search_files(db, "submarine") == []


# --- page (static/app.js in Node) ------------------------------------------------------------

def library_page(db, code: str, query: str = "", search=None):
    companies = [dict(r) for r in db.execute("SELECT id, name FROM companies ORDER BY name")]
    data = {"library": [{"company": c, "docs": list_documents(c["id"], conn=db), "metrics": None, "open": False}
                        for c in companies], "search": search}
    return render(f"""state.library = DATA.library; state.libraryQuery = {query!r}; state.librarySearch = DATA.search;
        {code}""", data)


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_library_shows_a_search_box_and_a_summary_under_each_file(db, library):
    html = library_page(db, "libraryBodyHtml()")
    assert 'id="library-search"' in html and 'placeholder="Search files' in html
    assert html.count('class="doc-summary"') == 9  # 7 ClearClaim files, 2 Kitebridge files
    assert "Pitch deck, Dec 1, 2025: 85 paying customers" in html


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_typing_filters_by_file_name_company_type_and_summary(db, library):
    def files(query):
        html = library_page(db, "libraryListHtml()", query)
        return html.count('class="doc-name"'), html

    assert files("q2")[0] == 1  # file name
    count, html = files("finance")  # company: the CRM file has no "finance" in its name or summary
    assert count == 2 and "kitebridge_crm.csv" in html and "clearclaim" not in html
    count, html = files("CRM records")  # type
    assert count == 2 and "clearclaim_crm.csv" in html and "kitebridge_crm.csv" in html
    count, html = files("addressable market")  # summary
    assert count == 1 and DECK in html
    count, html = files("gross margin")
    assert count == 1
    count, html = files("submarine")
    assert count == 0 and "Press Enter to search inside the files" in html


@pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
def test_enter_shows_which_files_mention_the_words_and_how_often(db, library):
    search = {"query": "gross margin", "results": search_files(db, "gross margin")}
    html = library_page(db, "librarySearchHtml()", "gross margin", search)
    assert html.index(DECK) < html.index("clearclaim_meeting_notes.md")
    text = " ".join(html.split())
    assert "2 files mention “gross margin”" in text
    assert "2 mentions" in text and "1 mention" in text and "p.5 ¶2, p.9 ¶3" in text
    none = library_page(db, "librarySearchHtml()", "submarine", {"query": "submarine", "results": []})
    assert "No file mentions “submarine”" in " ".join(none.split())


def test_summary_stays_on_one_line():
    style = (PROJECT_ROOT / "static" / "style.css").read_text()
    rule = style[style.index(".doc-summary"):].split("}")[0]
    assert "white-space: nowrap" in rule and "text-overflow: ellipsis" in rule and "overflow: hidden" in rule


def test_a_bare_number_keeps_the_unit_its_label_does_not_name(db):
    company_id = db.execute("INSERT INTO companies(name) VALUES ('Tidewell Insurance')").lastrowid
    doc = ingest_document(db, company_id, "tidewell_deck.pdf", "deck", "2026-07-15",
                          (SAMPLES / "company_a" / "tidewell_deck.pdf").read_bytes())["document_id"]
    store_metrics(db, company_id, doc, [("other", "Average time from registration to first quote", "4",
                                         "Average time from registration")])
    db.execute("UPDATE metrics SET unit = 'minutes'")
    assert document_summaries(db, company_id)[doc] == (
        "Pitch deck, Jul 15, 2026: average time from registration to first quote: 4 minutes")
