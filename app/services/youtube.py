"""YouTube ingestion: video metadata + description + best-effort English captions via yt-dlp.

Recipe channels usually put the recipe in the description; when they don't, the auto-captions
transcript is the fallback. Both feed the same LLM extractor as the web path (docs/04, docs/05).

yt-dlp is a heavy import and does network I/O, so it lives behind :func:`fetch`; the pure parsing
helpers (:func:`parse_info`, :func:`pick_caption_url`, :func:`json3_to_text`) take plain dicts and
are what the tests exercise - no network, no yt-dlp import.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

# Preference order for a caption track. "en-orig" is yt-dlp's original-language auto track.
_CAPTION_LANGS = ("en", "en-US", "en-GB", "en-orig")
_CAPTION_TIMEOUT = 10.0


class YoutubeError(RuntimeError):
    """Fetching or reading a YouTube video failed."""


@dataclass(frozen=True, slots=True)
class YoutubeData:
    video_id: str
    title: str
    description: str
    uploader: str | None
    thumbnail_url: str | None
    duration_seconds: int | None
    captions: str | None

    def prompt_text(self) -> str:
        """The text handed to the LLM: title, channel, description, and transcript if present."""
        parts = [f"YouTube video title: {self.title}"]
        if self.uploader:
            parts.append(f"Channel: {self.uploader}")
        parts.append("\nVideo description:\n" + (self.description or "(no description)"))
        if self.captions:
            parts.append("\nAuto-generated transcript:\n" + self.captions)
        return "\n".join(parts)

    def to_json(self) -> str:
        return json.dumps(
            {
                "video_id": self.video_id,
                "title": self.title,
                "description": self.description,
                "uploader": self.uploader,
                "thumbnail_url": self.thumbnail_url,
                "duration_seconds": self.duration_seconds,
                "has_captions": bool(self.captions),
            }
        )


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_info(info: dict[str, Any]) -> YoutubeData:
    """Build YoutubeData from a yt-dlp info dict (no captions text yet)."""
    title = str(info.get("title") or "").strip()
    if not title:
        raise YoutubeError("the video has no title")
    return YoutubeData(
        video_id=str(info.get("id") or "").strip(),
        title=title,
        description=str(info.get("description") or "").strip(),
        uploader=_clean(info.get("uploader") or info.get("channel")),
        thumbnail_url=_clean(info.get("thumbnail")),
        duration_seconds=_as_int(info.get("duration")),
        captions=None,
    )


def pick_caption_url(info: dict[str, Any]) -> str | None:
    """Choose an English json3 caption URL, preferring manual subtitles over auto-captions."""
    tracks: dict[str, Any] = dict(info.get("automatic_captions") or {})
    tracks.update(info.get("subtitles") or {})  # manual subtitles win for the same language
    for lang in _CAPTION_LANGS:
        for entry in tracks.get(lang) or []:
            if entry.get("ext") == "json3" and entry.get("url"):
                return str(entry["url"])
    return None


def json3_to_text(payload: dict[str, Any]) -> str:
    """Flatten YouTube's json3 caption payload (events -> segs -> utf8) into plain text."""
    lines: list[str] = []
    for event in payload.get("events") or []:
        text = "".join(str(seg.get("utf8", "")) for seg in event.get("segs") or []).strip()
        if text:
            lines.append(text)
    return " ".join(lines)


def fetch(url: str) -> YoutubeData:
    """Fetch a video's metadata and best-effort captions. Raises YoutubeError on failure."""
    info = _extract_info(url)
    data = parse_info(info)
    captions = _best_effort_captions(info)
    return replace(data, captions=captions) if captions else data


def _extract_info(url: str) -> dict[str, Any]:
    from yt_dlp import YoutubeDL  # lazy: heavy, network (CONVENTIONS 4)

    options = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many error types
        raise YoutubeError(f"yt-dlp could not read the video: {exc}") from exc
    if not isinstance(info, dict):
        raise YoutubeError("yt-dlp returned no video info")
    return info


def _best_effort_captions(info: dict[str, Any]) -> str | None:
    url = pick_caption_url(info)
    if not url:
        return None
    try:
        import httpx  # lazy (CONVENTIONS 4)

        resp = httpx.get(url, timeout=_CAPTION_TIMEOUT)
        resp.raise_for_status()
        return json3_to_text(resp.json()) or None
    except Exception:  # captions are optional - never fail the whole ingest over them
        return None
