import time

from utils.task_store import TaskStore


def test_create_returns_queued_record():
    record = TaskStore(ttl_seconds=3600).create("t1")
    assert record["task_id"] == "t1"
    assert record["status"] == "queued"
    assert record["created_at"] <= record["updated_at"]


def test_update_merges_fields_and_bumps_updated_at():
    store = TaskStore()
    store.create("t1")
    before = store.get("t1")["updated_at"]
    time.sleep(0.01)
    updated = store.update("t1", status="downloading", percent=12.5)
    assert updated["status"] == "downloading"
    assert updated["percent"] == 12.5
    assert updated["updated_at"] > before


def test_get_unknown_returns_none():
    assert TaskStore().get("nope") is None


def test_update_unknown_returns_none():
    assert TaskStore().update("nope", status="done") is None


def test_prune_deletes_expired_terminal_records():
    store = TaskStore(ttl_seconds=10)
    store.create("t1")
    store.update("t1", status="done")
    assert store.prune_expired(now=time.time() + 11) == 1
    assert store.get("t1") is None


def test_prune_marks_expired_running_records_failed():
    store = TaskStore(ttl_seconds=10)
    store.create("t1")
    store.update("t1", status="downloading")
    assert store.prune_expired(now=time.time() + 11) == 0
    record = store.get("t1")
    assert record["status"] == "error"
    assert "interrupted" in record["error"]


def test_prune_keeps_fresh_records():
    store = TaskStore(ttl_seconds=10)
    store.create("t1")
    assert store.prune_expired(now=time.time()) == 0
    assert store.get("t1") is not None


def test_mark_file_served_deletes_file(tmp_path):
    store = TaskStore()
    store.create("t1")
    out = tmp_path / "video.mp4"
    out.write_bytes(b"x")
    store.update("t1", status="done", extra={"filepath": str(out)})
    store.mark_file_served("t1")
    assert not out.exists()


def test_mark_file_served_ignores_missing_file(tmp_path):
    store = TaskStore()
    store.create("t1")
    store.update("t1", extra={"filepath": str(tmp_path / "missing.mp4")})
    store.mark_file_served("t1")  # must not raise
