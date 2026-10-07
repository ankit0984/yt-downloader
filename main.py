import asyncio
import logging
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse

from config.config import get_settings
from schema.api.schema import (
    DownloadRequest,
    DownloadResponse,
    FormatResponse,
    TaskStatusResponse,
)
from utils.download_runner import cleanup_old_files, run_download
from utils.downloader import get_formats
from utils.task_store import TaskStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ytdl.api")

settings = get_settings()

CLEANUP_INTERVAL_SECONDS = 60 * 60  # replaces the Celery beat schedule


class _TtlCache:
    """Tiny in-process TTL cache (replaces the Redis formats cache)."""

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.time():
            del self._entries[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = (time.time() + self._ttl, value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.DOWNLOAD_DIR, exist_ok=True)
    app.state.store = TaskStore(ttl_seconds=settings.TASK_TTL_SECONDS)
    app.state.formats_cache = _TtlCache(settings.FORMATS_CACHE_SECONDS)
    app.state.executor = ThreadPoolExecutor(max_workers=settings.MAX_CONCURRENT_DOWNLOADS)

    async def cleanup_loop():
        while True:
            try:
                removed = await run_in_threadpool(
                    cleanup_old_files, settings.DOWNLOAD_DIR, settings.MAX_FILE_AGE_HOURS
                )
                if removed:
                    logger.info("Cleanup removed %d old file(s)", removed)
                app.state.store.prune_expired()
            except Exception:
                logger.exception("Cleanup sweep failed")
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

    sweep = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        sweep.cancel()
        app.state.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="YT Downloader", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Rate limiting (in-process storage: one container instance) ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    lambda request, exc: PlainTextResponse("Too many requests", status_code=429),
)

# --- Authentication ---
api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)


def _valid_key(key: Optional[str]) -> bool:
    if not key:
        return False
    # Constant-time comparison against every configured key.
    return any(secrets.compare_digest(key, k) for k in settings.API_KEYS)


def verify_api_key(key: Optional[str] = Depends(api_key_header)):
    if not _valid_key(key):
        raise HTTPException(403, "Invalid API Key")
    return key


# ---------- Endpoints ----------
@app.get("/ping")
async def ping():
    """Container readiness probe (Cloudflare `pingEndpoint` default)."""
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/formats", response_model=FormatResponse, dependencies=[Depends(verify_api_key)])
@limiter.limit(settings.RATE_LIMIT)
async def formats(request: Request, url: str = Query(..., min_length=1)):
    """Return all available video/audio qualities for a URL."""
    cache: _TtlCache = request.app.state.formats_cache
    cached = cache.get(url)
    if cached is not None:
        return cached

    try:
        # yt-dlp is blocking network I/O — never run it on the event loop.
        payload = await run_in_threadpool(get_formats, url)
    except Exception as exc:
        msg = str(exc)
        if "not a bot" in msg or "Sign in to confirm" in msg:
            raise HTTPException(
                429,
                "YouTube is blocking this server as a bot. Set COOKIES_FROM_BROWSER "
                "or COOKIE_FILE in the backend config (see README).",
            )
        if "PO token" in msg or "No downloadable formats" in msg:
            raise HTTPException(
                429,
                "YouTube withheld the formats (PO token required). Configure a PO "
                "token provider (see README, 'YouTube access').",
            )
        raise HTTPException(400, f"Could not read this URL: {msg}")

    cache.set(url, payload)
    return payload


@app.post("/download", response_model=DownloadResponse, dependencies=[Depends(verify_api_key)])
@limiter.limit(settings.RATE_LIMIT)
async def start_download(request: Request, req: DownloadRequest):
    """Start a background download and return a task_id to track it."""
    task_id = str(uuid.uuid4())
    store: TaskStore = request.app.state.store
    store.create(task_id)
    request.app.state.executor.submit(
        run_download, req.url, req.format_id, task_id, req.audio_only, store
    )
    return {"task_id": task_id, "status": "queued"}


@app.get("/status/{task_id}", response_model=TaskStatusResponse, response_model_exclude_none=True, dependencies=[Depends(verify_api_key)])
async def status(task_id: str, request: Request):
    """Polling endpoint for live progress (replaces the WebSocket).

    `response_model_exclude_none` keeps the payload minimal: unset progress
    fields are omitted rather than serialized as null.
    """
    record = request.app.state.store.get(task_id)
    if record is None:
        return {"task_id": task_id, "status": "unknown"}
    return record


@app.get("/download/{task_id}/file", dependencies=[Depends(verify_api_key)])
async def download_file(task_id: str, request: Request):
    """Serve the finished file once, then delete it from the container."""
    record = request.app.state.store.get(task_id)
    if record is None:
        raise HTTPException(404, "Unknown or expired task")

    if record.get("status") != "done":
        raise HTTPException(409, f"Download not ready (status: {record.get('status')})")

    filepath = (record.get("extra") or {}).get("filepath")
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(404, "File not found (it may have been cleaned up)")

    return FileResponse(
        filepath,
        filename=os.path.basename(filepath),
        background=BackgroundTask(request.app.state.store.mark_file_served, task_id),
    )
