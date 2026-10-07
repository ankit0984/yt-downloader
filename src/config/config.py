from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = .../youtube_downloader (this file is at src/config/config.py)
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Directory for in-flight downloads. A relative value is resolved against
    # the repo root. Docker/Cloudflare set this to /tmp/ytdl. Files here are
    # deleted after the client fetches them (or by the hourly sweep).
    DOWNLOAD_DIR: str = "downloads"

    # --- Lifecycle ---
    MAX_FILE_AGE_HOURS: int = 24          # sweep stale files after N hours
    TASK_TTL_SECONDS: int = 60 * 60       # task record eviction (since last update)
    FORMATS_CACHE_SECONDS: int = 300      # cache /formats lookups
    MAX_CONCURRENT_DOWNLOADS: int = 2     # size of the download thread pool

    # --- Security ---
    API_KEY_HEADER: str = "X-API-Key"
    API_KEYS: list[str] = ["change-me-in-production"]
    RATE_LIMIT: str = "20/minute"

    # --- CORS (Flutter web builds / any browser client) ---
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # --- yt-dlp auth (to bypass "Sign in to confirm you're not a bot") ---
    COOKIES_FROM_BROWSER: Optional[str] = None
    COOKIE_FILE: Optional[str] = None
    YTDLP_PLAYER_CLIENTS: Optional[str] = None
    YTDLP_PO_TOKEN: Optional[str] = None

    @field_validator("DOWNLOAD_DIR")
    @classmethod
    def _resolve_download_dir(cls, v: str) -> str:
        path = Path(v)
        if not path.is_absolute():
            path = REPO_ROOT / path
        return str(path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
