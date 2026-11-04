"""Upload suggestions, consistency warnings and live issue counts."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.intake import guess_doc_date, guess_doc_type
from app.llm import FakeLLM, LLMAuthError, LLMError
from app.main import app, get_chat_llm, get_extract_llm
from tests.helpers import TIDEWELL_RULES, fake_extractor

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
NEUTRAL = b"SYNTHETIC SAMPLE. We sell software to dentists.\n\nGrowth has been steady."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as c:
        for name in ("Tidewell Insurance", "Kitebridge Finance"):
            c.post("/api/companies", json={"name": name})
        yield c


def analyze(client, filename, data):
    return client.post("/api/uploads/analyze", files={"file": (filename, data, "application/octet-stream")})


def model_says(name):
    fake = FakeLLM(default={"company_name": name})
    app.dependency_overrides[get_chat_llm] = lambda: fake
    return fake


def warn(client, chosen, suggested=None, mentioned=(), filename=""):
    r = client.post("/api/uploads/warnings", json={"chosen_company": chosen, "suggested_company": suggested,
                                                   "mentioned": list(mentioned), "filename": filename})
    assert r.status_code == 200
    return r.json()


# --- suggesting the company ------------------------------------------------------------

def test_company_from_file_name_without_calling_the_model(client):
    fake = model_says("Should Not Be Used")
    body = analyze(client, "tidewell_update_2026-08.txt", (SAMPLES / "company_a/tidewell_update_2026-08.txt").read_bytes()).json()
    assert body["company"]["name"] == "Tidewell Insurance"
    assert body["company"]["existing"] is True and body["company"]["source"] == "file name"
    assert body["warnings"] == []
    assert fake.calls == []


def test_company_from_content_without_calling_the_model(client):
    fake = model_says("Should Not Be Used")
    body = analyze(client, "notes.md", (SAMPLES / "company_b/kitebridge_meeting_notes.md").read_bytes()).json()
    assert body["company"]["name"] == "Kitebridge Finance" and body["company"]["source"] == "file content"
    assert fake.calls == []


def test_unmatched_file_calls_the_model_and_suggests_a_new_company(client):
    fake = model_says("Northwind Dental")
    body = analyze(client, "deck.txt", b"Northwind Dental pitch.\n\nWe sell software to dentists.").json()
    assert body["company"] == {"id": None, "name": "Northwind Dental", "existing": False, "source": "model"}
    assert len(fake.calls) == 1 and "Northwind Dental pitch" in fake.calls[0]["prompt"]


def test_model_name_that_is_not_in_the_file_is_ignored(client):
    model_says("Made Up Holdings")
    body = analyze(client, "deck.txt", NEUTRAL).json()
    assert body["company"] == {"id": None, "name": None, "existing": False, "source": "none"}


def test_model_finding_no_company_leaves_the_choice_to_the_user(client):
    model_says(None)
    assert analyze(client, "deck.txt", NEUTRAL).json()["company"]["source"] == "none"


@pytest.mark.parametrize("error, message", [
    (LLMAuthError("API key invalid or missing. Check .env and restart the server."),
     "Model call failed: API key invalid or missing. Check .env and restart the server."),
    (LLMError("Could not reach Anthropic API: Connection error."),
     "Model call failed: Could not reach Anthropic API: Connection error."),
])
def test_model_failure_is_reported_but_does_not_block_the_upload(client, error, message):
    def fail(call):
        raise error
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM(default=fail)
    r = analyze(client, "deck.txt", NEUTRAL)
    assert r.status_code == 200
    assert r.json()["company"] == {"id": None, "name": None, "existing": False, "source": "none", "model_error": message}

    cid = client.get("/api/companies").json()[0]["id"]
    stored = client.post(f"/api/companies/{cid}/documents", data={"doc_type": "notes"},
                         files={"file": ("deck.txt", NEUTRAL, "text/plain")})
    assert stored.status_code == 201


def test_model_finding_nothing_is_not_reported_as_a_failure(client):
    model_says(None)
    assert "model_error" not in analyze(client, "deck.txt", NEUTRAL).json()["company"]


def test_program_match_has_no_model_error_even_if_the_model_is_down(client):
    def fail(call):
        raise LLMError("down")
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM(default=fail)
    assert "model_error" not in analyze(client, "tidewell_notes.txt", b"Some notes.").json()["company"]


def test_analyze_stores_nothing(client):
    analyze(client, "tidewell_x.txt", b"Tidewell notes.")
    assert all(c["documents"] == 0 for c in client.get("/api/companies").json())


@pytest.mark.parametrize("filename, data, message", [
    ("deck.pptx", b"x", "Unsupported file format"),
    ("crm.csv", b"date,contact\n2026-01-01,Dana\n", "missing required CRM column"),
    ("empty.txt", b"", "is empty"),
])
def test_unreadable_files_fail_before_the_dialog(client, filename, data, message):
    r = analyze(client, filename, data)
    assert r.status_code == 400 and message in r.json()["detail"]


# --- type and date ---------------------------------------------------------------------

@pytest.mark.parametrize("filename, text, expected", [
    ("tidewell_crm.csv", "", "crm"),
    ("tidewell_deck.pdf", "", "deck"),
    ("seed_pitch.md", "", "deck"),
    ("meeting_2026.txt", "", "notes"),
    ("tidewell_update_2026-08.txt", "", "notes"),
    ("file.md", "Attendees: Dana", "notes"),
    ("file.txt", "This pitch deck covers", "deck"),
    ("file.pdf", "", "deck"),
    ("file.txt", "", "notes"),
])
def test_guess_doc_type(filename, text, expected):
    assert guess_doc_type(filename, text)[0] == expected


@pytest.mark.parametrize("filename, text, rows, expected", [
    ("crm.csv", "", [{"date": "2026-06-05"}, {"date": "2026-07-22"}, {"date": None}], "2026-07-22"),
    ("notes.md", "Date: 2026-07-22. Attendees: Dana", [], "2026-07-22"),
    ("notes.md", "Meeting dated July 22, 2026.", [], "2026-07-22"),
    ("notes_2026-08-10.txt", "No date inside.", [], "2026-08-10"),
    ("update_2026-08.txt", "No date inside.", [], "2026-08-01"),
    ("notes.md", "Call on June 5, 2026 with Dana.", [], "2026-06-05"),
    ("notes.md", "As of June 30, 2026, we had 80 customers.", [], None),  # a metric's date, not the file's
    ("notes.md", "Nothing here.", [], None),
    ("notes.md", "Date: 2026-13-45", [], None),
])
def test_guess_doc_date(filename, text, rows, expected):
    assert guess_doc_date(filename, text, rows)[0] == expected


def test_sample_files_get_the_expected_suggestions(client):
    expected = [("company_a/tidewell_crm.csv", "Tidewell Insurance", "crm", "2026-07-22"),
                ("company_a/tidewell_meeting_notes.md", "Tidewell Insurance", "notes", "2026-07-22"),
                ("company_a/tidewell_deck.pdf", "Tidewell Insurance", "deck", None),
                ("company_b/kitebridge_meeting_notes.md", "Kitebridge Finance", "notes", "2026-04-20")]
    for path, company, doc_type, doc_date in expected:
        body = analyze(client, Path(path).name, (SAMPLES / path).read_bytes()).json()
        assert (body["company"]["name"], body["doc_type"], body["doc_date"]) == (company, doc_type, doc_date), path


# --- consistency warnings --------------------------------------------------------------

def kinds(warnings):
    return [w["kind"] for w in warnings]


def test_file_mentioning_another_company_warns_when_analyzed(client):
    """The file name says Tidewell, the content talks about Kitebridge."""
    body = analyze(client, "tidewell_notes.txt", b"Call with Kitebridge Finance about their credit line.").json()
    assert body["company"]["name"] == "Tidewell Insurance"
    assert [w["message"] for w in body["warnings"]] == \
        ["This file mentions Kitebridge Finance, but you are adding it to Tidewell Insurance."]


def test_choosing_another_company_than_the_one_in_the_file_warns(client):
    body = analyze(client, "notes.txt", b"Kitebridge Finance call notes.").json()
    assert body["company"]["name"] == "Kitebridge Finance" and body["warnings"] == []
    warnings = warn(client, "Tidewell Insurance", "Kitebridge Finance", [m["name"] for m in body["mentioned"]])
    assert [w["message"] for w in warnings] == \
        ["This file mentions Kitebridge Finance, but you are adding it to Tidewell Insurance."]


def test_file_mentioning_several_companies_warns_for_any_choice(client):
    body = analyze(client, "notes.txt", b"Tidewell Insurance, Tidewell again, and also Kitebridge Finance.").json()
    assert body["company"]["name"] == "Tidewell Insurance"
    assert {m["name"] for m in body["mentioned"]} == {"Tidewell Insurance", "Kitebridge Finance"}
    assert kinds(body["warnings"]) == ["multiple_companies"]
    assert "Tidewell Insurance" in body["warnings"][0]["message"] and "Kitebridge Finance" in body["warnings"][0]["message"]


def test_user_changing_the_company_warns(client):
    # Suggested from the file name only: the content names no company.
    warnings = warn(client, "Kitebridge Finance", "Tidewell Insurance", [])
    assert [w["message"] for w in warnings] == \
        ["This file looks like it belongs to Tidewell Insurance, but you are adding it to Kitebridge Finance."]
    assert kinds(warn(client, "Kitebridge Finance", "Tidewell Insurance", ["Tidewell Insurance", "Kitebridge Finance"])) == \
        ["company_changed", "multiple_companies"]


def test_keeping_the_suggestion_has_no_warning(client):
    assert warn(client, "Tidewell Insurance", "Tidewell Insurance", ["Tidewell Insurance"]) == []
    assert warn(client, "tidewell insurance", "Tidewell Insurance", ["Tidewell Insurance"]) == []
    assert warn(client, "", "Tidewell Insurance", []) == []


def test_new_company_with_a_similar_name_warns(client):
    assert kinds(warn(client, "Tidewell Inc", None, [])) == ["similar_company"]
    assert warn(client, "Northwind Dental", None, []) == []


def test_replacing_a_file_warns(client):
    cid = client.get("/api/companies").json()[1]["id"]
    name = client.get("/api/companies").json()[1]["name"]
    client.post(f"/api/companies/{cid}/documents", files={"file": ("a.txt", b"Some text.", "text/plain")},
                data={"doc_type": "notes"})
    assert kinds(warn(client, name, name, [], "a.txt")) == ["replaces_file"]
    assert warn(client, name, name, [], "b.txt") == []


def test_warnings_do_not_block_the_upload(client):
    """Adding a Kitebridge file to Tidewell still works if the user continues."""
    cid = next(c["id"] for c in client.get("/api/companies").json() if c["name"].startswith("Tidewell"))
    r = client.post(f"/api/companies/{cid}/documents", data={"doc_type": "notes"},
                    files={"file": ("k.txt", b"Kitebridge Finance call notes.", "text/plain")})
    assert r.status_code == 201


# --- issue counts ------------------------------------------------------------------------

def upload_sample(client, cid, path, doc_type):
    return client.post(f"/api/companies/{cid}/documents", data={"doc_type": doc_type},
                       files={"file": (Path(path).name, (SAMPLES / path).read_bytes(), "text/plain")}).json()


def test_issue_counts_follow_uploads_and_deletes(client):
    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default=fake_extractor(TIDEWELL_RULES))
    cid = next(c["id"] for c in client.get("/api/companies").json() if c["name"].startswith("Tidewell"))

    def counts():
        return next(i for i in client.get("/api/issues").json() if i["id"] == cid)

    assert (counts()["missing"], counts()["total"]) == (4, 4)  # nothing uploaded yet

    upload_sample(client, cid, "company_a/tidewell_deck.pdf", "deck")
    upload_sample(client, cid, "company_a/tidewell_meeting_notes.md", "notes")
    upload_sample(client, cid, "company_a/tidewell_crm.csv", "crm")
    before = counts()
    assert (before["conflicts"], before["missing"], before["unclear_basis"]) == (0, 1, 1)  # revenue; $1.2M
    assert before["total"] == 2

    fourth = upload_sample(client, cid, "company_a/tidewell_update_2026-08.txt", "notes")
    after = counts()
    assert (after["conflicts"], after["missing"], after["unclear_basis"]) == (1, 1, 1)  # 60 vs 80
    assert after["total"] == before["total"] + 1

    client.delete(f"/api/documents/{fourth['document_id']}")
    assert counts() == before


def test_issue_details_have_value_file_location_and_quote(client):
    app.dependency_overrides[get_extract_llm] = lambda: FakeLLM(default=fake_extractor(TIDEWELL_RULES))
    cid = next(c["id"] for c in client.get("/api/companies").json() if c["name"].startswith("Tidewell"))
    for path, doc_type in [("company_a/tidewell_deck.pdf", "deck"), ("company_a/tidewell_meeting_notes.md", "notes"),
                           ("company_a/tidewell_update_2026-08.txt", "notes")]:
        upload_sample(client, cid, path, doc_type)
    checks = client.get(f"/api/companies/{cid}/checks").json()
    entries = [s for c in checks["conflicts"] for s in c["sources"]] + checks["unclear_basis"]
    assert len(entries) == 3
    for entry in entries:
        assert entry["value_raw"] and entry["filename"] and entry["loc_label"] and entry["quote"]
