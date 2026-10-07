"""In-memory task registry — replaces Redis state + pub/sub.

Single-process by design: the Cloudflare Container runs exactly one instance
(see wrangler.jsonc, max_instances = 1), so a locked dict is sufficient and
survives only as long as the container process lives.
"""

import os
import threading
import time
from typing import Any, Optional

# Terminal statuses: once a task reaches one of these it will not change again.
TERMINAL_STATUSES = {"done", "error"}


class TaskStore:
    def __init__(self, ttl_seconds: int = 3600):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    def create(self, task_id: str, status: str = "queued") -> dict[str, Any]:
        now = time.time()
        record: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._records[task_id] = record
            return dict(record)

    def update(self, task_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = time.time()
            return dict(record)

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            record = self._records.get(task_id)
            return dict(record) if record is not None else None

    def prune_expired(self, now: Optional[float] = None) -> int:
        """Expire stale records (e.g. after a container restart).

        Terminal records are deleted; non-terminal ones are marked failed so
        clients polling them see a definite outcome and can retry. Active
        downloads refresh `updated_at` on every progress event, so they are
        never pruned while making progress. Returns the number deleted.
        """
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            for task_id in list(self._records):
                record = self._records[task_id]
                if now - record["updated_at"] <= self._ttl:
                    continue
                if record["status"] in TERMINAL_STATUSES:
                    del self._records[task_id]
                    removed += 1
                else:
                    record.update(
                        status="error",
                        error="Task expired or was interrupted",
                        updated_at=now,
                    )
        return removed

    def mark_file_served(self, task_id: str) -> None:
        """Delete the task's output file after it has been served (best effort)."""
        record = self.get(task_id)
        if not record:
            return
        filepath = (record.get("extra") or {}).get("filepath")
        if filepath:
            try:
                os.remove(filepath)
            except FileNotFoundError:
                pass
