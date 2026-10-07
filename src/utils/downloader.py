import yt_dlp

from config.config import get_settings


def base_ydl_opts() -> dict:
    """Common yt-dlp options shared by extraction and downloading.

    Injects cookie auth when configured so YouTube's "confirm you're not a bot"
    gate is satisfied. Used by both get_formats() and the download worker.
    """
    settings = get_settings()
    opts: dict = {"quiet": True, "no_warnings": True}
    if settings.COOKIE_FILE:
        opts["cookiefile"] = settings.COOKIE_FILE
    if settings.COOKIES_FROM_BROWSER:
        # yt-dlp wants a tuple: (browser[, profile, keyring, container]).
        opts["cookiesfrombrowser"] = tuple(settings.COOKIES_FROM_BROWSER.split(":"))

    yt_args: dict = {}
    if settings.YTDLP_PLAYER_CLIENTS:
        yt_args["player_client"] = [c.strip() for c in settings.YTDLP_PLAYER_CLIENTS.split(",") if c.strip()]
    if settings.YTDLP_PO_TOKEN:
        yt_args["po_token"] = [t.strip() for t in settings.YTDLP_PO_TOKEN.split(",") if t.strip()]
    if yt_args:
        opts["extractor_args"] = {"youtube": yt_args}
    return opts


def get_formats(url: str) -> dict:
    """Return video metadata (title, duration, thumbnail) plus all usable formats.

    Video formats are reported as `has_video=True` AND `has_audio=True`: YouTube
    serves high-res video without sound (DASH), so on download we merge the chosen
    video stream with the best audio, producing a playable mp4. Audio-only streams
    are listed separately (`has_video=False`).
    """
    # format="all" keeps the full format list while stopping yt-dlp's default
    # selector from aborting extraction when only storyboards are returned.
    opts = {**base_ydl_opts(), "format": "all"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    video_by_height: dict = {}   # keyed by resolution height -> best format
    audio_by_abr: dict = {}      # keyed by audio bitrate -> best format

    for f in info.get("formats", []):
        has_video = f.get("vcodec", "none") != "none"
        # Some HLS manifests (e.g. Apple's advanced example) declare audio
        # renditions with vcodec="none" but include no codec string, so yt-dlp
        # omits the acodec key entirely. An explicit vcodec="none" still means
        # the stream is audio-only.
        has_audio = f.get("acodec", "none") != "none" or f.get("vcodec", "none") == "none"
        if f.get("format_note") == "storyboard" or not (has_video or has_audio):
            continue

        filesize = f.get("filesize") or f.get("filesize_approx")

        if has_video:
            height = f.get("height") or 0
            quality = f.get("format_note") or (f"{height}p" if height else f.get("resolution")) or "video"
            item = {
                "format_id": f["format_id"],
                "ext": "mp4",          # merged output is mp4
                "quality": quality,
                "has_video": True,
                "has_audio": True,     # guaranteed after audio merge on download
                "filesize": filesize,
                "size_display": format_size(filesize),
            }
            key = height or quality
            if key not in video_by_height or (filesize or 0) > (video_by_height[key][1] or 0):
                video_by_height[key] = (item, filesize, height)
        else:
            abr = f.get("abr") or 0
            quality = f"{round(abr)}kbps" if abr else (f.get("format_note") or "audio only")
            item = {
                "format_id": f["format_id"],
                "ext": f.get("ext", ""),
                "quality": quality,
                "has_video": False,
                "has_audio": True,
                "filesize": filesize,
                "size_display": format_size(filesize),
            }
            key = round(abr) if abr else quality
            if key not in audio_by_abr or (filesize or 0) > (audio_by_abr[key][1] or 0):
                audio_by_abr[key] = (item, filesize, abr)

    videos = [v[0] for v in sorted(video_by_height.values(), key=lambda x: x[2] or 0, reverse=True)]
    audios = [a[0] for a in sorted(audio_by_abr.values(), key=lambda x: x[2] or 0, reverse=True)]

    if not videos and not audios:
        # Only storyboards/no real streams — almost always YouTube withholding
        # formats behind a PO token. See README ("YouTube access").
        raise RuntimeError(
            "No downloadable formats were returned. YouTube likely requires a PO "
            "token for this request — configure a PO token provider (see README)."
        )

    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "formats": videos + audios,
    }


def format_size(size):
    if not size:
        return "unknown"
    gb = size / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    return f"{size / (1024 ** 2):.2f} MB"
