import time

import pytest
from fastapi.testclient import TestClient

import main

API_KEY = "change-me-in-production"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a scratch download dir before lifespan runs.
    monkeypatch.setattr(main.settings, "DOWNLOAD_DIR", str(tmp_path))
    # The developer's .env may set API_KEYS; pin the test key for hermetic auth.
    monkeypatch.setattr(main.settings, "API_KEYS", [API_KEY])
    with TestClient(main.app) as c:
        yield c


def _headers():
    return {"X-API-Key": API_KEY}


def test_ping(client):
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_auth_required_everywhere(client):
    assert client.get("/formats", params={"url": "https://v"}).status_code == 403
    assert client.get("/status/abc").status_code == 403
    assert client.get("/download/abc/file").status_code == 403
    assert client.post("/download", json={"url": "https://v"}).status_code == 403


def test_formats_cached_across_requests(client, monkeypatch):
    calls = {"n": 0}

    def fake_get_formats(url):
        calls["n"] += 1
        return {"title": "T", "duration": 1, "thumbnail": None, "formats": []}

    monkeypatch.setattr(main, "get_formats", fake_get_formats)

    first = client.get("/formats", params={"url": "https://v"}, headers=_headers())
    second = client.get("/formats", params={"url": "https://v"}, headers=_headers())
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["title"] == "T"
    assert calls["n"] == 1  # second call served from cache


def test_download_and_status_flow(client, monkeypatch):
    def fake_run_download(url, format_id, task_id, audio_only, store):
        store.update(task_id, status="done", percent=100.0, filename="v.mp4",
                     extra={"filepath": "/nonexistent"})

    monkeypatch.setattr(main, "run_download", fake_run_download)

    started = client.post(
        "/download", json={"url": "https://v", "format_id": "137"}, headers=_headers()
    )
    assert started.status_code == 200
    body = started.json()
    assert body["status"] == "queued"
    task_id = body["task_id"]

    status = {}
    for _ in range(100):
        status = client.get(f"/status/{task_id}", headers=_headers()).json()
        if status["status"] == "done":
            break
        time.sleep(0.01)
    assert status["status"] == "done"
    assert status["filename"] == "v.mp4"


def test_status_unknown_task(client):
    r = client.get("/status/does-not-exist", headers=_headers())
    assert r.status_code == 200
    assert r.json() == {"task_id": "does-not-exist", "status": "unknown"}


def test_download_rejects_non_http_url(client):
    r = client.post("/download", json={"url": "not-a-url"}, headers=_headers())
    assert r.status_code == 422


def test_file_unknown_task(client):
    assert client.get("/download/nope/file", headers=_headers()).status_code == 404


def test_file_not_ready(client):
    client.app.state.store.create("t-queued")
    r = client.get("/download/t-queued/file", headers=_headers())
    assert r.status_code == 409


def test_file_served_then_deleted(client, tmp_path):
    path = tmp_path / "out.mp4"
    path.write_bytes(b"video-bytes")
    store = client.app.state.store
    store.create("t-done")
    store.update("t-done", status="done", filename="out.mp4",
                 extra={"filepath": str(path)})

    r = client.get("/download/t-done/file", headers=_headers())
    assert r.status_code == 200
    assert r.content == b"video-bytes"
    assert not path.exists()  # BackgroundTask deleted it after serving
    assert client.get("/download/t-done/file", headers=_headers()).status_code == 404
