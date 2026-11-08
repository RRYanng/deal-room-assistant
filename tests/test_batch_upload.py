"""Batch upload: several files and companies, partial failure, replacement, refusal, background work."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.llm import FakeLLM, LLMError
from app.main import MAX_UPLOAD_BYTES, app, get_extract_llm
from tests.helpers import KITEBRIDGE_RULES, TIDEWELL_RULES, fake_extractor

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
NOTES = b"SYNTHETIC SAMPLE. We sell software to dentists.\n\nGrowth has been steady."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def companies(client):
    return {name: client.post("/api/companies", json={"name": name}).json()["id"]
            for name in ("Tidewell Insurance", "Kitebridge Finance")}


def row(filename, content=NOTES, company_id=None, new_company=None, doc_type="notes", doc_date=None):
    return {"filename": filename, "content": content, "company_id": company_id,
            "new_company": new_company, "doc_type": doc_type, "doc_date": doc_date}


def post_batch(client, rows):
    files = [("files", (r["filename"], r["content"])) for r in rows]
    items = [{k: r[k] for k in ("company_id", "new_company", "doc_type", "doc_date")} for r in rows]
    return client.post("/api/uploads/batch", files=files, data={"items": json.dumps(items)})


def wait_for_batch(client, batch_id):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        body = client.get(f"/api/uploads/batch/{batch_id}").json()
        if body["finished"]:
            return body
        time.sleep(0.02)
    raise AssertionError("The batch did not finish.")


def run_batch(client, rows):
    r = post_batch(client, rows)
    assert r.status_code == 202, r.text
    return wait_for_batch(client, r.json()["batch_id"])


def filenames(client, company_id):
    return [d["filename"] for d in client.get(f"/api/companies/{company_id}/documents").json()]


def sample(path):
    return (SAMPLES / path).read_bytes()


# --- several files, several companies ----------------------------------------------------

def test_files_go_to_their_own_companies_and_metrics_are_extracted(client, companies):
    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default=fake_extractor(TIDEWELL_RULES + KITEBRIDGE_RULES))
    tidewell, kitebridge = companies["Tidewell Insurance"], companies["Kitebridge Finance"]
    body = run_batch(client, [
        row("tidewell_meeting_notes.md", sample("company_a/tidewell_meeting_notes.md"), tidewell),
        row("tidewell_update_2026-08.txt", sample("company_a/tidewell_update_2026-08.txt"), tidewell, doc_date="2026-08-10"),
        row("kitebridge_meeting_notes.md", sample("company_b/kitebridge_meeting_notes.md"), kitebridge),
    ])

    assert [r["status"] for r in body["rows"]] == ["done", "done", "done"]
    assert [r["company"]["name"] for r in body["rows"]] == ["Tidewell Insurance", "Tidewell Insurance", "Kitebridge Finance"]
    assert all(r["extraction"]["status"] == "ok" and r["error"] is None for r in body["rows"])
    assert filenames(client, tidewell) == ["tidewell_meeting_notes.md", "tidewell_update_2026-08.txt"]
    assert filenames(client, kitebridge) == ["kitebridge_meeting_notes.md"]

    docs = client.get(f"/api/companies/{tidewell}/documents").json()
    assert [d["doc_date"] for d in docs] == [None, "2026-08-10"]
    # The two Tidewell files disagree (60 vs 80), so the program's check sees the new conflict.
    assert len(client.get(f"/api/companies/{tidewell}/checks").json()["conflicts"]) == 1
    issues = {c["name"]: c for c in client.get("/api/issues").json()}
    assert issues["Tidewell Insurance"]["conflicts"] == 1 and issues["Kitebridge Finance"]["conflicts"] == 0


def test_rows_with_the_same_new_company_create_it_once(client):
    body = run_batch(client, [row("a.md", new_company="Acme Robotics"), row("b.md", new_company="  acme robotics ")])
    assert [r["status"] for r in body["rows"]] == ["done", "done"]
    assert [c["name"] for c in client.get("/api/companies").json()] == ["Acme Robotics"]
    assert body["rows"][0]["company"] == body["rows"][1]["company"]
    assert filenames(client, body["rows"][0]["company"]["id"]) == ["a.md", "b.md"]


# --- partial failure ---------------------------------------------------------------------

def test_a_bad_file_fails_alone_and_says_why(client, companies):
    cid = companies["Tidewell Insurance"]
    body = run_batch(client, [
        row("good1.md", company_id=cid),
        row("broken.pdf", b"this is not a pdf", cid, doc_type="deck"),
        row("empty.md", b"", cid),
        row("good2.md", company_id=cid),
    ])
    assert [r["status"] for r in body["rows"]] == ["done", "failed", "failed", "done"]
    assert "broken.pdf" in body["rows"][1]["error"] and "empty.md is empty" in body["rows"][2]["error"]
    assert filenames(client, cid) == ["good1.md", "good2.md"]


def test_a_file_over_the_size_limit_fails_alone(client, companies):
    cid = companies["Tidewell Insurance"]
    body = run_batch(client, [row("huge.md", b"x" * (MAX_UPLOAD_BYTES + 1), cid), row("ok.md", company_id=cid)])
    assert [r["status"] for r in body["rows"]] == ["failed", "done"]
    assert "20 MB" in body["rows"][0]["error"]
    assert filenames(client, cid) == ["ok.md"]


def test_a_company_that_does_not_exist_fails_that_row_only(client, companies):
    body = run_batch(client, [row("a.md", company_id=999), row("b.md", company_id=companies["Kitebridge Finance"])])
    assert [r["status"] for r in body["rows"]] == ["failed", "done"]
    assert "999" in body["rows"][0]["error"]


def test_an_unreadable_file_does_not_create_its_new_company(client):
    body = run_batch(client, [row("broken.pdf", b"not a pdf", new_company="Ghost Ltd", doc_type="deck")])
    assert body["rows"][0]["status"] == "failed"
    assert client.get("/api/companies").json() == []


def test_failed_extraction_keeps_the_file_and_the_other_rows_go_on(client, companies):
    def reply(call):
        if "fails.md" in call["prompt"]:
            raise LLMError("API key invalid or missing.")
        return {"metrics": []}

    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default=reply)
    cid = companies["Tidewell Insurance"]
    body = run_batch(client, [row("fails.md", company_id=cid), row("works.md", company_id=cid)])

    assert [r["status"] for r in body["rows"]] == ["failed", "done"]
    assert "Stored, but metrics were not extracted" in body["rows"][0]["error"]
    assert "API key invalid or missing" in body["rows"][0]["error"]
    assert filenames(client, cid) == ["fails.md", "works.md"]


def test_an_unexpected_error_fails_one_row_and_the_batch_still_finishes(client, companies):
    def reply(call):
        if "crashes.md" in call["prompt"]:
            raise RuntimeError("boom")
        return {"metrics": []}

    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default=reply)
    cid = companies["Tidewell Insurance"]
    body = run_batch(client, [row("crashes.md", company_id=cid), row("works.md", company_id=cid)])
    assert [r["status"] for r in body["rows"]] == ["failed", "done"]
    assert "RuntimeError" in body["rows"][0]["error"]


# --- same file name ----------------------------------------------------------------------

def test_same_file_name_replaces_the_stored_file(client, companies):
    cid = companies["Tidewell Insurance"]
    first = run_batch(client, [row("notes.md", b"Old text.", cid)])["rows"][0]
    assert first["replaced"] is False
    second = run_batch(client, [row("notes.md", b"New text.", cid, doc_date="2026-09-01")])["rows"][0]
    assert second["status"] == "done" and second["replaced"] is True

    docs = client.get(f"/api/companies/{cid}/documents").json()
    assert [(d["filename"], d["doc_date"]) for d in docs] == [("notes.md", "2026-09-01")]
    chunks = client.get(f"/api/documents/{docs[0]['id']}/chunks").json()
    assert [c["text"] for c in chunks] == ["New text."]


def test_replacement_without_a_date_keeps_the_old_date(client, companies):
    """tidewell_deck.pdf dated 2026-07-15, replaced in a batch with the date left empty."""
    cid = companies["Tidewell Insurance"]
    run_batch(client, [row("tidewell_deck.pdf", sample("company_a/tidewell_deck.pdf"), cid, doc_type="deck",
                           doc_date="2026-07-15")])
    second = run_batch(client, [row("tidewell_deck.pdf", sample("company_a/tidewell_deck.pdf"), cid, doc_type="deck")])
    assert second["rows"][0]["status"] == "done" and second["rows"][0]["replaced"] is True

    docs = client.get(f"/api/companies/{cid}/documents").json()
    assert [(d["filename"], d["doc_date"]) for d in docs] == [("tidewell_deck.pdf", "2026-07-15")]


def test_same_file_name_twice_in_a_batch_keeps_the_later_file(client, companies):
    cid = companies["Tidewell Insurance"]
    body = run_batch(client, [row("notes.md", b"First.", cid), row("notes.md", b"Second.", cid)])

    assert [r["status"] for r in body["rows"]] == ["failed", "done"]
    assert "replaced" in body["rows"][0]["error"]
    docs = client.get(f"/api/companies/{cid}/documents").json()
    assert len(docs) == 1
    assert client.get(f"/api/documents/{docs[0]['id']}/chunks").json()[0]["text"] == "Second."


def test_same_file_name_in_two_companies_stays_separate(client, companies):
    body = run_batch(client, [row("notes.md", company_id=companies["Tidewell Insurance"]),
                              row("notes.md", company_id=companies["Kitebridge Finance"])])
    assert [(r["status"], r["replaced"]) for r in body["rows"]] == [("done", False), ("done", False)]


# --- refused before anything is stored ---------------------------------------------------

@pytest.mark.parametrize("no_company", [{}, {"new_company": ""}, {"new_company": "   "}])
def test_a_row_without_a_company_refuses_the_whole_batch(client, companies, no_company):
    cid = companies["Tidewell Insurance"]
    r = post_batch(client, [row("good.md", company_id=cid), row("unknown.md", **no_company)])
    assert r.status_code == 400
    assert "unknown.md" in r.json()["detail"] and "good.md" not in r.json()["detail"]
    assert filenames(client, cid) == []
    assert len(client.get("/api/companies").json()) == 2


def test_items_must_match_the_files(client, companies):
    files = [("files", ("a.md", NOTES)), ("files", ("b.md", NOTES))]
    item = {"company_id": companies["Tidewell Insurance"], "doc_type": "notes"}
    r = client.post("/api/uploads/batch", files=files, data={"items": json.dumps([item])})
    assert r.status_code == 400 and "does not match" in r.json()["detail"]
    r = client.post("/api/uploads/batch", files=files, data={"items": "not json"})
    assert r.status_code == 400 and "does not match" in r.json()["detail"]
    assert filenames(client, companies["Tidewell Insurance"]) == []


def test_unknown_batch_is_not_found(client):
    assert client.get("/api/uploads/batch/nope").status_code == 404


# --- extraction runs in the background ---------------------------------------------------

def test_the_app_stays_usable_while_metrics_are_extracted(client, companies):
    """While the model is held on the first file, the upload has already returned and all files are stored."""
    release = threading.Event()
    started = threading.Event()

    def held(call):
        started.set()
        assert release.wait(30)
        return {"metrics": []}

    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default=held)
    cid = companies["Tidewell Insurance"]
    try:
        r = post_batch(client, [row("one.md", company_id=cid), row("two.md", company_id=cid)])
        assert r.status_code == 202
        batch_id = r.json()["batch_id"]
        assert started.wait(30)

        body = client.get(f"/api/uploads/batch/{batch_id}").json()
        assert body["finished"] is False
        assert [r["status"] for r in body["rows"]] == ["analyzing", "stored"]
        assert filenames(client, cid) == ["one.md", "two.md"]
        assert client.get("/api/issues").status_code == 200
    finally:
        release.set()
    assert [r["status"] for r in wait_for_batch(client, batch_id)["rows"]] == ["done", "done"]


def test_the_response_has_one_row_per_file(client, companies):
    r = post_batch(client, [row("a.md", company_id=companies["Tidewell Insurance"])])
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"batch_id", "finished", "rows"}
    assert body["rows"][0]["filename"] == "a.md"
    assert set(body["rows"][0]) == {"filename", "status", "error", "company", "document_id", "replaced", "chunks", "extraction"}
    wait_for_batch(client, body["batch_id"])
