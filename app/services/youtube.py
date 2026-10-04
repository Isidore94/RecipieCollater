"""YouTube ingestion: video metadata + description + best-effort English captions via yt-dlp.

Recipe channels usually put the recipe in the description; when they don't, the auto-captions
transcript is the fallback. Both feed the same LLM extractor as the web path (docs/04, docs/05).

yt-dlp is a heavy import and does network I/O, so it lives behind :func:`fetch`; the pure parsing
helpers (:func:`parse_info`, :func:`pick_caption_url`, :func:`json3_to_text`) take plain dicts and
are what the tests exercise - no network, no yt-dlp import.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

# Preference order for a caption track. "en-orig" is yt-dlp's original-language auto track.
_CAPTION_LANGS = ("en", "en-US", "en-GB", "en-orig")
_CAPTION_TIMEOUT = 10.0

# Below this many characters of description + captions the video has little to extract from, so
# it is worth a second yt-dlp call for the pinned/top comments (many creators pin the recipe).
_THIN_CHARS = 600
# yt-dlp's max_comments extractor arg: total, parents, replies, replies-per-thread (no replies).
_COMMENT_FETCH_LIMIT = ["30", "all", "0", "0"]
_MAX_COMMENT_CHARS = 1500  # per comment
_MAX_COMMENTS_TOTAL_CHARS = 3000  # across the whole prompt block
_MAX_TOP_COMMENTS = 3  # liked comments kept beyond any pinned / channel-owner ones
_MAX_SELECTED_COMMENTS = 6


# Failure classes. ``blocked`` is YouTube refusing automated reads from this network (it clears
# on its own, so the worker retries on a long schedule); ``unavailable`` is a video that is gone
# or private (retrying cannot help); ``other`` is everything else.
KIND_BLOCKED = "blocked"
KIND_UNAVAILABLE = "unavailable"
KIND_OTHER = "other"

_BLOCKED_MARKERS = (
    "confirm you're not a bot",
    "not a bot",
    "too many requests",
    "http error 429",
    "rate-limited",
    "rate limited",
    "ip is likely being blocked",
)
_UNAVAILABLE_MARKERS = (
    "video unavailable",
    "private video",
    "this video is private",
    "has been removed",
    "no longer available",
    "account associated with this video has been terminated",
    "video has been deleted",
    "copyright",
)


def classify_error(message: str) -> str:
    """Classify a yt-dlp failure message as blocked / unavailable / other (pure, text-only).

    Curly apostrophes are folded first (YouTube sends both). "This content isn't available" is
    yt-dlp's wording for a rate-limited session, so it counts as blocked unless the message also
    says the video itself is gone or private.
    """
    text = message.lower().replace("\u2019", "'").replace("\u2018", "'")
    if any(marker in text for marker in _BLOCKED_MARKERS):
        return KIND_BLOCKED
    if any(marker in text for marker in _UNAVAILABLE_MARKERS):
        return KIND_UNAVAILABLE
    if "this content isn't available" in text:
        return KIND_BLOCKED
    return KIND_OTHER


class YoutubeError(RuntimeError):
    """Fetching or reading a YouTube video failed; ``kind`` says which class of failure."""

    def __init__(self, message: str, *, kind: str = KIND_OTHER) -> None:
        super().__init__(message)
        self.kind = kind


_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
# A line that opens like an ingredient: a number / fraction, or a bare quantity word.
_INGREDIENT_LINE_RE = re.compile(
    r"^\s*[-*\u2022]?\s*(?:\d|[\u00bc-\u00be\u2150-\u215e]|(?:a|an|one|half|pinch|dash)\s+\w)",
    re.IGNORECASE,
)
THIN_DESCRIPTION_CHARS = 200
THIN_DESCRIPTION_MIN_INGREDIENT_LINES = 3


def description_is_thin(description: str) -> bool:
    """True when a video description carries too little recipe to extract from on its own.

    After stripping URLs and hashtags (channel promo, not recipe), a description is thin if it is
    under ``THIN_DESCRIPTION_CHARS`` characters or has fewer than
    ``THIN_DESCRIPTION_MIN_INGREDIENT_LINES`` ingredient-looking lines.
    """
    text = _HASHTAG_RE.sub("", _URL_RE.sub("", description or "")).strip()
    if len(text) < THIN_DESCRIPTION_CHARS:
        return True
    lines = sum(1 for line in text.splitlines() if _INGREDIENT_LINE_RE.match(line))
    return lines < THIN_DESCRIPTION_MIN_INGREDIENT_LINES


@dataclass(frozen=True, slots=True)
class Chapter:
    start_seconds: int
    title: str


@dataclass(frozen=True, slots=True)
class Comment:
    text: str
    author: str | None = None
    pinned: bool = False
    by_uploader: bool = False
    like_count: int = 0


@dataclass(frozen=True, slots=True)
class YoutubeData:
    video_id: str
    title: str
    description: str
    uploader: str | None
    thumbnail_url: str | None
    duration_seconds: int | None
    captions: str | None
    chapters: tuple[Chapter, ...] = ()
    comments: tuple[Comment, ...] = ()

    @property
    def source_basis(self) -> str:
        """Where the recipe text came from: "captions" only when the description was thin and a
        transcript exists (the model then worked from spoken, auto-generated text)."""
        if self.captions and description_is_thin(self.description):
            return "captions"
        return "description"

    def prompt_text(self) -> str:
        """The text handed to the LLM: title, channel, description, and transcript if present."""
        parts = [f"YouTube video title: {self.title}"]
        if self.uploader:
            parts.append(f"Channel: {self.uploader}")
        parts.append("\nVideo description:\n" + (self.description or "(no description)"))
        if self.chapters:
            lines = [f"{format_timestamp(c.start_seconds)} {c.title}" for c in self.chapters]
            parts.append("\nChapters:\n" + "\n".join(lines))
        if self.captions:
            parts.append("\nAuto-generated transcript:\n" + self.captions)
        if self.comments:
            parts.append("\n" + comments_prompt_block(self.comments))
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
                "source_basis": self.source_basis,
                "chapters": [
                    {"start_seconds": c.start_seconds, "title": c.title} for c in self.chapters
                ],
                "comments_used": [
                    {
                        "text": c.text, "author": c.author, "pinned": c.pinned,
                        "by_uploader": c.by_uploader, "like_count": c.like_count,
                    }
                    for c in self.comments
                ],
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
    description = str(info.get("description") or "").strip()
    return YoutubeData(
        video_id=str(info.get("id") or "").strip(),
        title=title,
        description=description,
        uploader=_clean(info.get("uploader") or info.get("channel")),
        thumbnail_url=_clean(info.get("thumbnail")),
        duration_seconds=_as_int(info.get("duration")),
        captions=None,
        chapters=parse_chapters(info.get("chapters")) or parse_description_chapters(description),
    )


def format_timestamp(seconds: int) -> str:
    """m:ss under an hour, h:mm:ss beyond (matches how YouTube shows chapters)."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _finish_chapters(found: Iterable[Chapter]) -> tuple[Chapter, ...]:
    """Sort by start, drop duplicate start times (first title wins)."""
    seen: set[int] = set()
    out: list[Chapter] = []
    for chapter in sorted(found, key=lambda c: c.start_seconds):
        if chapter.start_seconds in seen:
            continue
        seen.add(chapter.start_seconds)
        out.append(chapter)
    return tuple(out)


def parse_chapters(raw: Any) -> tuple[Chapter, ...]:
    """Read yt-dlp's ``chapters`` list ([{start_time, end_time, title}]); junk is skipped."""
    if not isinstance(raw, list):
        return ()
    found: list[Chapter] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(float(str(entry.get("start_time"))))
        except (TypeError, ValueError):
            continue
        title = _clean(entry.get("title"))
        if title and start >= 0:
            found.append(Chapter(start, title))
    return _finish_chapters(found)


# "0:45 Sear the chicken", "[1:02:10] - Serve", "(2:30) Bake". Leading timestamps only.
_DESC_TS_LINE = re.compile(
    r"^\s*[\[(]?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[\])]?\s*[-\u2013\u2014:.)]*\s*(\S.*)$"
)


def parse_description_chapters(description: str) -> tuple[Chapter, ...]:
    """Chapters from explicit "m:ss title" lines in a description (when yt-dlp found none).

    Needs at least two lines with strictly ascending times, which keeps a stray timing line
    from becoming a chapter list.
    """
    found: list[Chapter] = []
    for line in description.splitlines():
        match = _DESC_TS_LINE.match(line)
        if not match:
            continue
        hours, minutes, secs, title = match.groups()
        if int(secs) > 59 or (hours is not None and int(minutes) > 59):
            continue
        start = int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
        found.append(Chapter(start, title.strip()))
    starts = [c.start_seconds for c in found]
    if len(found) < 2 or starts != sorted(set(starts)):
        return ()
    return tuple(found)


def is_thin(description: str | None, captions: str | None) -> bool:
    """True when description + captions carry so little text that comments are worth fetching."""
    return len((description or "").strip()) + len((captions or "").strip()) < _THIN_CHARS


def select_comments(
    raw: Any,
    *,
    max_total_chars: int = _MAX_COMMENTS_TOTAL_CHARS,
    max_top: int = _MAX_TOP_COMMENTS,
) -> tuple[Comment, ...]:
    """Pick the comments worth showing the model from yt-dlp's ``comments`` list.

    Pinned first, then the video author's own comments, then the top ``max_top`` of the rest by
    like count - all capped by total characters (each comment is clipped too).
    """
    if not isinstance(raw, list):
        return ()
    comments: list[Comment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = _clean(entry.get("text"))
        if not text:
            continue
        comments.append(
            Comment(
                text=text[:_MAX_COMMENT_CHARS],
                author=_clean(entry.get("author")),
                pinned=bool(entry.get("is_pinned")),
                by_uploader=bool(entry.get("author_is_uploader")),
                like_count=_as_int(entry.get("like_count")) or 0,
            )
        )
    priority = [c for c in comments if c.pinned]
    priority += [c for c in comments if c.by_uploader and not c.pinned]
    rest = sorted(
        (c for c in comments if not c.pinned and not c.by_uploader),
        key=lambda c: -c.like_count,  # stable: ties keep yt-dlp's order
    )
    chosen: list[Comment] = []
    used = 0
    for comment in [*priority, *rest[:max_top]][:_MAX_SELECTED_COMMENTS]:
        if used + len(comment.text) > max_total_chars:
            continue
        chosen.append(comment)
        used += len(comment.text)
    return tuple(chosen)


def comments_prompt_block(comments: Sequence[Comment]) -> str:
    lines = ["Pinned/top comments (written by viewers or the channel; a recipe may be here):"]
    for comment in comments:
        tag = "pinned" if comment.pinned else "channel owner" if comment.by_uploader else "top"
        lines.append(f"[{tag}] {comment.text}")
    return "\n".join(lines)


_NON_WORD = re.compile(r"[^a-z0-9]+")
_FRAMING_TITLES = {"intro", "introduction", "outro", "outtro", "conclusion", "ending", "end"}


def _norm(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


def assign_step_seconds(
    instructions: Sequence[str],
    model_seconds: Sequence[int | None],
    chapters: Sequence[Chapter],
    duration_seconds: int | None = None,
) -> list[int | None]:
    """Decide each step's ``video_seconds``.

    1. Timestamps the model supplied win (out-of-range ones are dropped).
    2. Otherwise a step whose text starts with a chapter title takes that chapter's start.
    3. Otherwise, when the step count is within one of the chapter count, steps map to chapters
       in order (an intro/outro chapter is set aside first if that makes the counts match).
    Anything else stays None - a wrong "Watch this step" link is worse than none.
    """
    count = len(instructions)
    given: list[int | None] = []
    for index in range(count):
        value = model_seconds[index] if index < len(model_seconds) else None
        ok = value is not None and value >= 0 and (
            duration_seconds is None or value <= duration_seconds
        )
        given.append(value if ok else None)
    if any(v is not None for v in given) or not chapters or count == 0:
        return given

    titled = [(_norm(c.title), c.start_seconds) for c in chapters if len(_norm(c.title)) >= 3]
    by_title: list[int | None] = []
    for text in instructions:
        normalized = _norm(text)
        by_title.append(next((s for t, s in titled if normalized.startswith(t)), None))
    if any(v is not None for v in by_title):
        return by_title

    trimmed = [c for c in chapters if _norm(c.title) not in _FRAMING_TITLES]
    for candidate in (list(chapters), trimmed):
        if len(candidate) >= 2 and len(candidate) == count:
            return [c.start_seconds for c in candidate]
    for candidate in (list(chapters), trimmed):
        if len(candidate) >= 2 and abs(len(candidate) - count) <= 1:
            last = len(candidate) - 1
            return [candidate[min(i, last)].start_seconds for i in range(count)]
    return [None] * count


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
    if captions:
        data = replace(data, captions=captions)
    if is_thin(data.description, data.captions):
        # Second yt-dlp call, only for thin videos: many creators pin the recipe as a comment.
        comments = select_comments(_best_effort_comments(url))
        if comments:
            data = replace(data, comments=comments)
    return data


def _extract_info(url: str) -> dict[str, Any]:
    from yt_dlp import YoutubeDL  # lazy: heavy, network (CONVENTIONS 4)

    options = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many error types
        raise YoutubeError(
            f"yt-dlp could not read the video: {exc}", kind=classify_error(str(exc))
        ) from exc
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


def _best_effort_comments(url: str) -> list[Any]:
    """Top-level comments via a second yt-dlp call. Optional - any failure yields no comments."""
    try:
        from yt_dlp import YoutubeDL  # lazy: heavy, network (CONVENTIONS 4)

        options = {
            "quiet": True, "no_warnings": True, "skip_download": True, "getcomments": True,
            "extractor_args": {"youtube": {"max_comments": _COMMENT_FETCH_LIMIT}},
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        comments = info.get("comments") if isinstance(info, dict) else None
        return comments if isinstance(comments, list) else []
    except Exception:
        return []
