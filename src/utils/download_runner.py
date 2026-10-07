"""Blocking yt-dlp download runner — replaces the Celery task.

Runs inside a ThreadPoolExecutor worker thread (see main.py). Progress is
written into the shared in-memory TaskStore instead of Redis pub/sub, and the
finished file stays in DOWNLOAD_DIR only until the client fetches it.
"""

import logging
import os
import time

import yt_dlp

from config.config import get_settings
from utils.downloader import base_ydl_opts
from utils.task_store import TaskStore

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5


def format_selector(format_id: str, audio_only: bool) -> str:
    """Build a yt-dlp format string.

    - audio_only: download just the chosen audio stream.
    - "best": best video+audio available.
    - a video format id: merge the chosen video with the best audio (falling back
      to the video alone if no audio can be merged) so the output always has sound.
    """
    if audio_only:
        return f"{format_id}/bestaudio"
    if format_id == "best":
        return "bestvideo+bestaudio/best"
    return f"{format_id}+bestaudio/{format_id}"


def _progress_hook(task_id: str, store: TaskStore):
    """yt-dlp calls this repeatedly during a download."""

    def hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            store.update(
                task_id,
                status="downloading",
                percent=round(downloaded / total * 100, 2) if total else 0.0,
                downloaded_bytes=downloaded,
                total_bytes=total or None,
                speed=d.get("speed"),
                eta=d.get("eta"),
                filename=os.path.basename(d.get("filename", "")),
            )
        elif status == "finished":
            # A stream finished downloading; yt-dlp may still merge video+audio.
            store.update(task_id, status="processing", percent=100.0)

    return hook


def _ydl_opts(task_id: str, format_id: str, audio_only: bool, store: TaskStore) -> dict:
    outtmpl = os.path.join(settings.DOWNLOAD_DIR, f"{task_id}_%(title)s.%(ext)s")
    opts = {
        **base_ydl_opts(),
        "format": format_selector(format_id, audio_only),
        "outtmpl": outtmpl,
        "progress_hooks": [_progress_hook(task_id, store)],
        "noprogress": True,
    }
    if not audio_only:
        opts["merge_output_format"] = "mp4"
    return opts


def run_download(
    url: str, format_id: str, task_id: str, audio_only: bool, store: TaskStore
) -> None:
    """Run one download+merge with retries, updating the store in place.

    Never raises: failures are recorded in the store as `error`.
    """
    os.makedirs(settings.DOWNLOAD_DIR, exist_ok=True)
    store.update(task_id, status="started")

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(task_id, format_id, audio_only, store)) as ydl:
                info = ydl.extract_info(url, download=True)

            # The reliable way to get the final (post-merge) path.
            requested = info.get("requested_downloads")
            if requested:
                filepath = requested[0].get("filepath") or requested[0].get("_filename")
            else:
                filepath = ydl.prepare_filename(info)

            if not filepath or not os.path.exists(filepath):
                raise FileNotFoundError("Downloaded file could not be located on disk")

            store.update(
                task_id,
                status="done",
                percent=100.0,
                filename=os.path.basename(filepath),
                extra={"filepath": filepath},
            )
            return
        except Exception as exc:
            logger.exception("Download attempt %d failed for task %s", attempt, task_id)
            if attempt <= MAX_RETRIES:
                store.update(
                    task_id,
                    status="retrying",
                    extra={"attempt": attempt, "reason": str(exc)},
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            store.update(task_id, status="error", error=str(exc))
            return


def cleanup_old_files(download_dir: str, max_age_hours: int) -> int:
    """Delete downloaded files older than max_age_hours. Returns count removed."""
    if not os.path.isdir(download_dir):
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for entry in os.scandir(download_dir):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except OSError:
            logger.warning("Could not remove %s", entry.path, exc_info=True)
    return removed
