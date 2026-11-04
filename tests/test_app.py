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
