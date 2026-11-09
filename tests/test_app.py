from fastapi.testclient import TestClient


def test_health_and_db_created(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert client.get("/").status_code == 200
    assert db_path.exists()


def test_ui_files_are_revalidated_by_the_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    from app.main import app

    with TestClient(app) as client:
        for path in ("/", "/static/app.js", "/static/style.css"):
            r = client.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path


def test_page_links_to_files_by_content_hash(tmp_path, monkeypatch):
    """A cached script from an older version must never run on a newer page."""
    import re

    from app.main import STATIC_DIR, app, asset_url

    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    with TestClient(app) as client:
        html = client.get("/").text
        for name in ("style.css", "app.js"):
            url = re.search(rf'"(/static/{name}\?v=\w+)"', html)
            assert url and url[1] == asset_url(name), name
            r = client.get(url[1])
            assert r.status_code == 200 and r.content == (STATIC_DIR / name).read_bytes()


def test_asset_url_changes_when_the_file_changes(tmp_path):
    from app.main import asset_url

    (tmp_path / "app.js").write_text("one")
    first = asset_url("app.js", tmp_path)
    assert asset_url("app.js", tmp_path) == first
    (tmp_path / "app.js").write_text("two")
    assert asset_url("app.js", tmp_path) != first


def test_ui_does_not_use_native_dialogs():
    """Some browsers block confirm(), alert() and prompt(); a blocked confirm() looks like a dead button."""
    import re

    from app.main import STATIC_DIR

    assert not re.search(r"(?<![\w.])(confirm|alert|prompt)\(", (STATIC_DIR / "app.js").read_text(encoding="utf-8"))


def test_upload_input_allows_several_files_and_each_row_gets_a_status():
    """The file picker only offers multi-select if the input says so."""
    from app.main import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="upload-file"' in html and 'accept=".pdf,.txt,.md,.csv" multiple' in html
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for label in ("Uploading", "Analyzing", "Done", "Failed"):
        assert f'"{label}"' in script


def test_the_page_never_shows_a_file_path_from_the_server():
    """Investors see what the checklist says, not where it is kept."""
    from app.config import PROJECT_ROOT
    for name in ("app.js", "index.html"):
        assert "config/" not in (PROJECT_ROOT / "static" / name).read_text(), name
