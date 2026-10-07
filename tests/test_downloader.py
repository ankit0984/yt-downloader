"""Tests for format listing (src/utils/downloader.py)."""

import utils.downloader as dl


# Mirrors Apple's advanced HLS example (img_bipbop_adv_example_fmp4): video
# renditions declare vcodec but no acodec; the audio renditions only declare
# vcodec="none" — their codec is unknown at extraction time, so yt-dlp omits
# the acodec key entirely.
FAKE_INFO = {
    "title": "HLS test",
    "duration": 600,
    "thumbnail": None,
    "formats": [
        {"format_id": "18", "vcodec": "avc1.42001e", "acodec": "mp4a.40.2",
         "height": 360, "format_note": "360p", "filesize": 1_000_000,
         "ext": "mp4"},
        {"format_id": "v1080", "vcodec": "avc1.64002a",
         "height": 1080, "format_note": "1080p", "filesize_approx": 2_000_000,
         "ext": "mp4"},
        {"format_id": "aud2-English", "vcodec": "none",
         "format_note": "English, high", "ext": "m4a"},
        {"format_id": "sb0", "vcodec": "none", "acodec": "none",
         "format_note": "storyboard", "ext": "mhtml"},
    ],
}


class _FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        assert download is False  # get_formats must never download
        return FAKE_INFO


def test_audio_only_rendition_with_unknown_codec_is_listed(monkeypatch):
    """Audio renditions without an acodec key must still be listed.

    Caught by Task 6 E2E verification: such renditions were dropped from
    /formats, so the audio-only download flow could not pick them.
    """
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", _FakeYDL)

    result = dl.get_formats("https://example.com/master.m3u8")

    by_id = {f["format_id"]: f for f in result["formats"]}
    assert "aud2-English" in by_id, "audio-only rendition was dropped from formats"
    assert by_id["aud2-English"]["has_video"] is False
    assert by_id["aud2-English"]["has_audio"] is True
    # Existing behavior must not regress:
    assert by_id["v1080"]["has_video"] is True
    assert "18" in by_id
    assert "sb0" not in by_id, "storyboards must stay filtered out"
