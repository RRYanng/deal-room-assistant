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
