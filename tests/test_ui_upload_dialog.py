"""The upload confirmation dialog: with no company found in the file nothing is chosen, not even the company
selected in the sidebar, and Add waits for a choice; a company found in the file is still chosen for you. A
material type guessed from the extension alone starts on "Other – choose a type". Front end only: the server's
suggestion from /api/uploads/analyze, shown by static/app.js in Node (tests/render_ui.js)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.llm import FakeLLM
from app.main import app, get_chat_llm
from tests.test_ui_dismissed import NODE, render

pytestmark = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
UNRELATED = b"Weekend plans.\n\nBuy milk, call the plumber, return the library books."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as c:
        for name in ("Tidewell Insurance", "ClearClaim AI", "Kitebridge Finance"):
            c.post("/api/companies", json={"name": name})
        yield c


def suggestion(client, filename, data):
    r = client.post("/api/uploads/analyze", files={"file": (filename, data, "application/octet-stream")})
    assert r.status_code == 200
    return r.json()


def dialog(client, s, code=""):
    """Opens the dialog on the suggestion with Tidewell Insurance selected in the sidebar."""
    companies = client.get("/api/companies").json()
    return render(f"""state.companies = DATA.companies;
        state.current = DATA.companies.find((c) => c.name === "Tidewell Insurance");
        openUploadDialog({{ name: DATA.s.filename }}, DATA.s); {code};
        ({{ company: $("upload-company").value, options: $("upload-company").innerHTML,
           why: $("upload-company-why").textContent, add: $("upload-confirm").textContent,
           disabled: $("upload-confirm").disabled, type: $("upload-type").value,
           typeWhy: $("upload-type-why").textContent }})""", {"companies": companies, "s": s})


def test_an_unrelated_file_chooses_no_company(client):
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM(default={"company_name": None})
    s = suggestion(client, "weekend.txt", UNRELATED)
    assert s["company"]["source"] == "none"
    out = dialog(client, s)
    assert out["company"] == ""  # not Tidewell Insurance, the company selected in the sidebar
    assert out["options"].startswith('<option value="">Choose a company or add a new one</option>')
    assert "+ New company…</option>" in out["options"]
    assert out["why"] == "No company found in this file."
    assert out["add"] == "Add" and out["disabled"] is True
    # the type is guessed from ".txt" alone: Other, not Pitch deck or Meeting notes
    assert out["type"] == "" and out["typeWhy"] == "The file name and content do not say. Choose a type."


def test_choosing_a_company_and_a_type_enables_add(client):
    app.dependency_overrides[get_chat_llm] = lambda: FakeLLM(default={"company_name": None})
    s = suggestion(client, "weekend.txt", UNRELATED)
    kitebridge = next(c["id"] for c in client.get("/api/companies").json() if c["name"] == "Kitebridge Finance")
    chosen = dialog(client, s, f'$("upload-company").value = "{kitebridge}"; renderUploadWarnings([])')
    assert chosen["add"] == "Add to Kitebridge Finance" and chosen["disabled"] is True  # still no type
    typed = dialog(client, s, f'''$("upload-company").value = "{kitebridge}"; renderUploadWarnings([]);
        $("upload-type").value = "notes"; $("upload-type").onchange()''')
    assert typed["add"] == "Add to Kitebridge Finance" and typed["disabled"] is False
    assert typed["typeWhy"] == ""  # chosen by you, not suggested


def test_a_clearclaim_sample_still_chooses_clearclaim(client):
    path = SAMPLES / "company_c" / "clearclaim_deck_2025-12.pdf"
    s = suggestion(client, path.name, path.read_bytes())
    clearclaim = next(c["id"] for c in client.get("/api/companies").json() if c["name"] == "ClearClaim AI")
    assert s["company"]["id"] == clearclaim and s["company"]["existing"]
    out = dialog(client, s)
    assert out["company"] == str(clearclaim)  # not Tidewell Insurance, the company selected in the sidebar
    assert out["add"] == "Add to ClearClaim AI" and out["disabled"] is False
    assert out["type"] == "deck" and out["typeWhy"] == "Suggested from: file name."


def test_the_other_choice_is_in_the_page_and_never_sent():
    html = (PROJECT_ROOT / "static" / "index.html").read_text()
    assert '<option value="">Other – choose a type</option>' in html
    app_js = (PROJECT_ROOT / "static" / "app.js").read_text()
    assert 'if (!name || !$("upload-type").value || !state.upload) return;' in app_js  # Add does nothing without a type
