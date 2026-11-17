"""Public demo (DEMO_MODE=true): a clean copy of the demo data on every start and every day, upload, delete and
runs turned off, and limits on questions and briefs per IP and for the whole site. Without DEMO_MODE nothing
changes."""

import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.db import connect, init_db
from app.main import app
from tests.test_ui_dismissed import NODE, render

OFF = "Turned off in the demo"
LIMIT = "Demo limit reached. Watch the walkthrough video instead."

TURNED_OFF = [
    ("post", "/api/companies", {"json": {"name": "New Co"}}),
    ("post", "/api/companies/1/documents", {"data": {"doc_type": "deck"}, "files": {"file": ("a.txt", b"x")}}),
    ("post", "/api/uploads/analyze", {"files": {"file": ("a.txt", b"x")}}),
    ("post", "/api/uploads/warnings", {"json": {"chosen_company": "X"}}),
    ("post", "/api/uploads/batch", {"data": {"items": "[]"}, "files": {"files": ("a.txt", b"x")}}),
    ("post", "/api/documents/1/extract", {}),
    ("delete", "/api/documents/1", {}),
    ("post", "/api/companies/1/claim-check", {"json": {}}),
    ("post", "/api/companies/1/monitor", {}),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.db"
    conn = connect(path)
    init_db(conn)
    with conn:
        conn.execute("INSERT INTO companies(id, name) VALUES (1, 'ClearClaim AI')")
    conn.close()
    return path


@pytest.fixture
def demo_env(tmp_path, source, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_SOURCE_DB", str(source))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "session.db"))
    for name in ("DEMO_ASKS_PER_IP", "DEMO_BRIEFS_PER_IP", "DEMO_MODEL_CALLS_PER_DAY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "session.db"


@pytest.fixture
def client(demo_env):
    with TestClient(app) as c:
        yield c
    app.state.__dict__.pop("demo_limiter", None)


def ask(client, ip, company_id=99):
    """A question from one visitor. Company 99 does not exist, so a question let through answers 404."""
    return client.post(f"/api/companies/{company_id}/ask", json={"question": "What is the margin?"},
                       headers={"X-Forwarded-For": ip})


# --- data -----------------------------------------------------------------------------------------------------

def test_each_start_serves_a_clean_copy_and_never_changes_the_source(demo_env, source):
    before = sha(source)
    with TestClient(app) as c:
        assert [x["name"] for x in c.get("/api/companies").json()] == ["ClearClaim AI"]
        c.put("/api/companies/1/memo-notes", json={"text": "Visitor notes"})
    conn = connect(demo_env)
    assert conn.execute("SELECT COUNT(*) FROM memo_notes").fetchone()[0] == 1
    conn.close()
    with TestClient(app):  # the next start drops what the last visitor did
        pass
    conn = connect(demo_env)
    assert conn.execute("SELECT COUNT(*) FROM memo_notes").fetchone()[0] == 0
    conn.close()
    assert sha(source) == before
    app.state.__dict__.pop("demo_limiter", None)


def test_the_daily_reset_replaces_the_data_under_an_open_connection(source, tmp_path):
    target = tmp_path / "session.db"
    demo.restore(source, target)
    conn = connect(target)
    with conn:
        conn.execute("UPDATE companies SET name = 'Changed by a visitor'")
    demo.restore(source, target)
    assert conn.execute("SELECT name FROM companies").fetchone()[0] == "ClearClaim AI"
    conn.close()


def test_the_demo_refuses_to_overwrite_its_own_source(source):
    with pytest.raises(RuntimeError, match="must not be the demo source"):
        demo.restore(source, source)


def test_the_reset_runs_at_midnight_utc():
    now = datetime(2026, 11, 17, 23, 0, tzinfo=timezone.utc)
    assert demo.seconds_to_midnight_utc(now) == 3600


# --- turned off -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,kwargs", TURNED_OFF)
def test_upload_delete_and_runs_are_turned_off(client, method, path, kwargs):
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 403 and r.json()["detail"] == OFF


@pytest.mark.parametrize("method,path,kwargs", TURNED_OFF)
def test_without_demo_mode_nothing_is_turned_off(tmp_path, monkeypatch, method, path, kwargs):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as c:
        r = getattr(c, method)(path, **kwargs)
    assert r.status_code not in (403, 429)


def test_browsing_reviews_and_memo_notes_still_work(client):
    assert client.get("/api/companies").status_code == 200
    assert client.get("/api/issues").status_code == 200
    assert client.get("/api/companies/1/memo").status_code == 200
    assert client.put("/api/companies/1/memo-notes", json={"text": "Looks promising"}).status_code == 200
    r = client.post("/api/companies/1/reviews", json={"fingerprint": "nope", "decision": "confirmed"})
    assert r.status_code not in (403, 429)  # reaches the review code, which finds no such item


def test_the_page_is_told_it_is_the_demo(client, monkeypatch):
    assert client.get("/api/ui").json()["demo"] is True
    monkeypatch.delenv("DEMO_MODE")
    assert client.get("/api/ui").json()["demo"] is False


# --- limits -----------------------------------------------------------------------------------------------------

def test_the_sixth_question_from_one_ip_is_stopped(client):
    for _ in range(5):
        assert ask(client, "203.0.113.7").status_code == 404  # let through to the app
    r = ask(client, "203.0.113.7")
    assert r.status_code == 429 and r.json()["detail"] == LIMIT
    assert ask(client, "198.51.100.2").status_code == 404  # another visitor still has questions


def test_questions_without_a_company_count_too(client):
    for _ in range(5):
        ask(client, "203.0.113.7")
    r = client.post("/api/ask", json={"question": "Hello"}, headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 429


def test_a_made_up_forwarded_address_does_not_reset_the_count(client):
    """The proxy adds the real address last; a visitor can only add entries before it."""
    for n in range(5):
        ask(client, f"10.0.0.{n}, 203.0.113.7")
    assert ask(client, "10.9.9.9, 203.0.113.7").status_code == 429


def test_railways_real_ip_header_is_used_first(client):
    for n in range(5):
        client.post("/api/companies/99/ask", json={"question": "Margin?"},
                    headers={"X-Real-IP": "203.0.113.7", "X-Forwarded-For": f"10.0.0.{n}"})
    r = client.post("/api/companies/99/ask", json={"question": "Margin?"},
                    headers={"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "10.0.0.99"})
    assert r.status_code == 429


def test_one_brief_per_ip(client):
    url = "/api/companies/99/brief"
    first = client.post(url, json={}, headers={"X-Forwarded-For": "203.0.113.7"})
    assert first.status_code == 404
    second = client.post(url, json={}, headers={"X-Forwarded-For": "203.0.113.7"})
    assert second.status_code == 429 and second.json()["detail"] == LIMIT
    assert ask(client, "203.0.113.7").status_code == 404  # questions are counted apart from briefs


def test_the_site_stops_after_the_daily_total(demo_env, monkeypatch):
    monkeypatch.setenv("DEMO_MODEL_CALLS_PER_DAY", "3")
    with TestClient(app) as c:
        assert [ask(c, f"203.0.113.{n}").status_code for n in range(3)] == [404, 404, 404]
        r = ask(c, "203.0.113.50")
        assert r.status_code == 429 and r.json()["detail"] == LIMIT
        assert c.post("/api/companies/99/brief", json={}, headers={"X-Forwarded-For": "203.0.113.51"}).status_code == 429
    app.state.__dict__.pop("demo_limiter", None)


def test_the_counts_start_again_each_utc_day():
    day = {"today": "2026-11-17"}
    limiter = demo.Limiter(demo.Limits(asks_per_ip=1, briefs_per_ip=1, calls_per_day=2), today=lambda: day["today"])
    assert limiter.take("a", "ask") and not limiter.take("a", "ask")
    assert limiter.take("b", "brief") and not limiter.take("c", "ask")  # the site total of 2 is reached
    day["today"] = "2026-11-18"
    assert limiter.take("a", "ask") and limiter.take("c", "ask")


def test_a_refused_request_is_not_counted():
    limiter = demo.Limiter(demo.Limits(asks_per_ip=1, briefs_per_ip=1, calls_per_day=2), today=lambda: "d")
    assert limiter.take("a", "ask")
    assert not limiter.take("a", "ask") and not limiter.take("a", "ask")
    assert limiter.take("b", "ask")  # a's refused questions did not use up the site total


# --- page -------------------------------------------------------------------------------------------------------

needs_node = pytest.mark.skipif(not NODE, reason="needs Node.js to run static/app.js")
PAGE = """ui.demo = {demo}; applyDemo(); claims.companyId = 1; claims.data = {{ running: null, run: {{}} }};
    syncClaimButtons(); mon.companyId = 1; mon.data = {{ running: null }}; renderMonitor();
    ({{ banner: !$("demo-banner").hidden, upload: $("mode-upload").disabled, uploadTitle: $("mode-upload").title,
       deck: $("claims-run").disabled, deckTitle: $("claims-run").title, run: $("monitor-run").disabled,
       runTitle: $("monitor-run").title, empty: $("chat").innerHTML,
       doc: documentHtml({{ id: 7, filename: "deck.pdf", doc_type: "deck", doc_date: "2025-12-01" }}) }})"""


@needs_node
def test_the_demo_page_shows_the_banner_and_greys_out_upload_and_runs():
    out = render(PAGE.format(demo="true"), None)
    assert out["banner"] and out["upload"] and out["deck"] and out["run"]
    assert out["uploadTitle"] == out["deckTitle"] == out["runTitle"] == OFF
    assert out["doc"].count(f'disabled title="{OFF}"') == 2  # re-extract and delete
    assert ">Ask about a company by name<" in out["empty"]  # the empty page does not suggest uploading


@needs_node
def test_without_demo_mode_the_page_is_unchanged():
    out = render(PAGE.format(demo="false"), None)
    assert not out["banner"] and not out["upload"] and not out["deck"] and not out["run"]
    assert out["uploadTitle"] == out["deckTitle"] == out["runTitle"] == ""
    assert "disabled" not in out["doc"]
    assert ">Upload files or ask about a company by name<" in out["empty"]


def test_the_banner_text_is_in_the_page():
    from app.config import PROJECT_ROOT
    html = (PROJECT_ROOT / "static" / "index.html").read_text()
    assert ">Demo with synthetic data. Upload and Run are turned off.</div>" in html
    assert html.count('<span class="demo-off">Turned off in the demo</span>') == 2


# --- the public demo database --------------------------------------------------------------------------------

def test_the_demo_reads_the_public_database_by_default(monkeypatch):
    monkeypatch.delenv("DEMO_SOURCE_DB", raising=False)
    assert demo.source_db().name == "demo_public.db"


def test_the_public_database_has_the_demo_content_and_no_question_logs():
    from app.rules import company_checks
    from app.config import PROJECT_ROOT
    path = PROJECT_ROOT / "data" / "demo_public.db"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    count = lambda sql: conn.execute(sql).fetchone()[0]
    assert {r[0] for r in conn.execute("SELECT DISTINCT run_id FROM agent_logs")} == \
        {"claims-run1", "claims-run1-competitors", "monitor-run1"}  # Deck check and Portfolio only
    assert count("SELECT COUNT(*) FROM reviews") == 0
    clearclaim = conn.execute("SELECT id FROM companies WHERE name = 'ClearClaim AI'").fetchone()[0]
    assert count(f"SELECT COUNT(*) FROM documents WHERE company_id = {clearclaim}") == 7
    assert count("SELECT COUNT(*) FROM claim_runs WHERE status = 'done'") == 1
    assert count("SELECT COUNT(*) FROM signals") == 4
    assert len(company_checks(conn, clearclaim)["conflicts"]) == 3
    conn.close()
