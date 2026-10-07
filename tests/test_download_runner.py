import os
import time
from pathlib import Path

import pytest

import utils.download_runner as dr
from utils.task_store import TaskStore


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Keep retry delays from slowing the suite down."""
    monkeypatch.setattr(dr.time, "sleep", lambda seconds: None)


def test_format_selector_audio_only():
    assert dr.format_selector("140", True) == "140/bestaudio"


def test_format_selector_best():
    assert dr.format_selector("best", False) == "bestvideo+bestaudio/best"


def test_format_selector_video_id_merges_audio():
    assert dr.format_selector("137", False) == "137+bestaudio/137"


class _FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL: fires progress hooks and writes a real file."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download):
        hook = self.opts["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10,
              "speed": 1.0, "eta": 1, "filename": "x.mp4"})
        path = Path(self.opts["outtmpl"].replace("%(title)s", "video").replace("%(ext)s", "mp4"))
        path.write_bytes(b"data")
        hook({"status": "finished"})
        return {"requested_downloads": [{"filepath": str(path)}]}

    def prepare_filename(self, info):
        raise AssertionError("not used when requested_downloads is present")


class _BoomYDL(_FakeYDL):
    def extract_info(self, url, download):
        raise RuntimeError("boom")


class _FlakyYDL(_FakeYDL):
    calls = 0

    def extract_info(self, url, download):
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError("flaky")
        return super().extract_info(url, download)


def test_run_download_success_updates_store_to_done(tmp_path, monkeypatch):
    monkeypatch.setattr(dr.settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(dr.yt_dlp, "YoutubeDL", _FakeYDL)
    store = TaskStore()
    store.create("t1")

    dr.run_download("https://example.com/v", "137", "t1", False, store)

    record = store.get("t1")
    assert record["status"] == "done"
    assert record["percent"] == 100.0
    assert record["filename"].endswith(".mp4")
    assert os.path.exists(record["extra"]["filepath"])


def test_run_download_retries_once_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(dr.settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(dr.yt_dlp, "YoutubeDL", _FlakyYDL)
    _FlakyYDL.calls = 0
    store = TaskStore()
    store.create("t1")

    dr.run_download("https://example.com/v", "137", "t1", False, store)

    assert store.get("t1")["status"] == "done"
    assert _FlakyYDL.calls == 2


def test_run_download_errors_after_retries_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(dr.settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(dr.yt_dlp, "YoutubeDL", _BoomYDL)
    store = TaskStore()
    store.create("t1")

    dr.run_download("https://example.com/v", "137", "t1", False, store)

    record = store.get("t1")
    assert record["status"] == "error"
    assert "boom" in record["error"]


def test_cleanup_old_files_removes_only_stale(tmp_path):
    old = tmp_path / "old.mp4"
    new = tmp_path / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))

    removed = dr.cleanup_old_files(str(tmp_path), max_age_hours=24)

    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_cleanup_old_files_missing_dir_is_noop(tmp_path):
    assert dr.cleanup_old_files(str(tmp_path / "nope"), max_age_hours=24) == 0


def test_run_download_records_error_when_download_dir_uncreatable(tmp_path, monkeypatch):
    # A file sits where the download directory would need to be created,
    # so os.makedirs must fail with an OSError ("never raises" contract).
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(dr.settings, "DOWNLOAD_DIR", str(blocker / "sub"))
    monkeypatch.setattr(dr.yt_dlp, "YoutubeDL", _FakeYDL)
    store = TaskStore()
    store.create("t1")

    dr.run_download("https://example.com/v", "137", "t1", False, store)

    record = store.get("t1")
    assert record["status"] == "error"
    assert record.get("extra") is None  # never reached a download attempt
