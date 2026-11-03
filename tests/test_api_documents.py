"""Upload API end to end: companies, upload, replacement, persistence across restarts."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    return path


@pytest.fixture
def client(db_path):
    with TestClient(app) as c:
        yield c


def make_company(client, name="Tidewell Insurance"):
    r = client.post("/api/companies", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(client, company_id, filename, content, doc_type, doc_date=None):
    data = {"doc_type": doc_type} | ({"doc_date": doc_date} if doc_date else {})
    return client.post(f"/api/companies/{company_id}/documents",
                       files={"file": (filename, content)}, data=data)


def upload_sample(client, company_id, rel_path, doc_type, doc_date="2026-07-15"):
    path = SAMPLES / rel_path
    return upload(client, company_id, path.name, path.read_bytes(), doc_type, doc_date)


def test_upload_pdf_stores_document_and_located_chunks(client):
    cid = make_company(client)
    r = upload_sample(client, cid, "company_a/tidewell_deck.pdf", "deck")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["replaced"] is False and body["chunks"] > 0

    docs = client.get(f"/api/companies/{cid}/documents").json()
    assert [(d["filename"], d["doc_type"], d["doc_date"]) for d in docs] == \
        [("tidewell_deck.pdf", "deck", "2026-07-15")]

    chunks = client.get(f"/api/documents/{body['document_id']}/chunks").json()
    traction = [c for c in chunks if "100 small businesses" in c["text"]]
    assert traction[0]["loc_label"] == "tidewell_deck.pdf p.3 ¶2"
    assert (traction[0]["page"], traction[0]["para"]) == (3, 2)


def test_same_filename_replaces_old_version_without_duplicates(client, db_path):
    cid = make_company(client)
    upload(client, cid, "notes.md", b"Old paragraph one.\n\nOld paragraph two.", "notes")
    r = upload(client, cid, "notes.md", b"New paragraph.", "notes", "2026-08-01")
    assert r.status_code == 201 and r.json()["replaced"] is True

    docs = client.get(f"/api/companies/{cid}/documents").json()
    assert len(docs) == 1 and docs[0]["doc_date"] == "2026-08-01"
    conn = connect(db_path)
    texts = [r["text"] for r in conn.execute("SELECT text FROM chunks")]
    conn.close()
    assert texts == ["New paragraph."]


def test_crm_replacement_does_not_duplicate_crm_records(client, db_path):
    cid = make_company(client)
    for _ in range(2):
        assert upload_sample(client, cid, "company_a/tidewell_crm.csv", "crm").status_code == 201
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 3
    conn.close()


def test_failed_replacement_keeps_old_version(client, db_path):
    cid = make_company(client)
    upload_sample(client, cid, "company_a/tidewell_crm.csv", "crm")
    r = upload(client, cid, "tidewell_crm.csv", b"date,summary\n2026-01-01,x\n", "crm")
    assert r.status_code == 400
    assert "missing required CRM column(s): contact, next_steps" in r.json()["detail"]
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM crm_records").fetchone()[0] == 3
    conn.close()


def test_same_filename_in_two_companies_is_kept_separately(client):
    a, b = make_company(client, "A"), make_company(client, "B")
    upload(client, a, "notes.md", b"About A.", "notes")
    upload(client, b, "notes.md", b"About B.", "notes")
    doc_a = client.get(f"/api/companies/{a}/documents").json()[0]
    doc_b = client.get(f"/api/companies/{b}/documents").json()[0]
    assert client.get(f"/api/documents/{doc_a['id']}/chunks").json()[0]["text"] == "About A."
    assert client.get(f"/api/documents/{doc_b['id']}/chunks").json()[0]["text"] == "About B."


def test_data_survives_server_restart(db_path):
    with TestClient(app) as c:
        cid = make_company(c)
        upload_sample(c, cid, "company_a/tidewell_meeting_notes.md", "notes")
    with TestClient(app) as c:  # new app startup, same database file
        companies = c.get("/api/companies").json()
        assert companies == [{"id": cid, "name": "Tidewell Insurance", "documents": 1}]
        doc = c.get(f"/api/companies/{cid}/documents").json()[0]
        assert doc["filename"] == "tidewell_meeting_notes.md" and doc["chunks"] == 7


def test_unsupported_format_returns_400_with_message(client):
    cid = make_company(client)
    r = upload(client, cid, "deck.docx", b"PK...", "deck")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Unsupported file format '.docx' for deck.")


def test_company_errors(client):
    assert client.post("/api/companies", json={"name": "  "}).status_code == 400
    make_company(client, "Dup")
    assert client.post("/api/companies", json={"name": "Dup"}).status_code == 409
    assert upload(client, 999, "a.md", b"x", "notes").status_code == 404


def test_replacement_never_reuses_document_or_chunk_ids(client):
    cid = make_company(client)
    first = upload(client, cid, "notes.md", b"Version one.", "notes").json()
    old_chunk = client.get(f"/api/documents/{first['document_id']}/chunks").json()[0]["id"]
    second = upload(client, cid, "notes.md", b"Version two.", "notes").json()
    new_chunk = client.get(f"/api/documents/{second['document_id']}/chunks").json()[0]["id"]
    assert second["document_id"] != first["document_id"]
    assert new_chunk != old_chunk
    assert client.get(f"/api/documents/{first['document_id']}/chunks").status_code == 404
