from typing import Any, List, Optional

from pydantic import BaseModel, field_validator


class FormatItem(BaseModel):
    format_id: str
    ext: str
    quality: str
    has_video: bool
    has_audio: bool
    filesize: Optional[int]
    size_display: str


class FormatResponse(BaseModel):
    title: Optional[str]
    duration: Optional[int]
    thumbnail: Optional[str] = None
    formats: List[FormatItem]


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "best"
    # Set true when the chosen format is an audio-only stream (has_video=False),
    # so the worker grabs just the audio instead of merging in video.
    audio_only: bool = False

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must be an http(s) URL")
        return v


class DownloadResponse(BaseModel):
    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    percent: Optional[float] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    speed: Optional[float] = None
    eta: Optional[int] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    # Any extra fields the worker publishes are preserved.
    extra: Optional[dict[str, Any]] = None
