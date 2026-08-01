"""Instagram ingestion: a reel's caption is the recipe.

Instagram refuses anonymous access to its private API. Measured 2026-07-27 on the deploy target,
yt-dlp 2026.07.04 answers *every* post -- valid shortcode or invented one -- with "Instagram sent
an empty media response ... use --cookies-from-browser". So this module never calls yt-dlp and
never holds an Instagram credential. Two caption sources instead, in order of reliability:

* HTML captured by the Apple Shortcut inside the family member's own logged-in Safari session.
  That path does not come through here at all -- the pipeline feeds it to the generic page-text
  extractor -- but it is why a private/followers-only post is still importable.
* The public ``/embed/captioned/`` page, which serves a *public* post's caption with no login.
  This is the only network call, and it is deliberately best-effort: Instagram may change the
  embed markup at any time, so :func:`parse_embed_html` tries several shapes and a miss is
  reported as a clean, actionable failure rather than an exception.

:func:`parse_embed_html` is pure and is what the tests exercise -- no network, no fixtures that
rot. :func:`fetch` is the only I/O and reuses the SSRF-hardened fetcher (CONVENTIONS security).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services import fetch as fetch_service

# Instagram renders this when a shortcode does not resolve for an anonymous viewer -- which covers
# both "deleted" and "private", since it will not admit which.
_UNAVAILABLE_MARKERS = (
    "may be broken, or the post may have been removed",
    "isn't available",
    "Sorry, this page",
)
_MAX_CAPTION_CHARS = 12_000


class InstagramError(RuntimeError):
    """An Instagram post could not be read anonymously."""


@dataclass(frozen=True, slots=True)
class InstagramData:
    shortcode: str
    caption: str
    author: str | None
    thumbnail_url: str | None

    def prompt_text(self) -> str:
        """The text handed to the LLM. The caption carries the whole recipe on Instagram."""
        parts = []
        if self.author:
            parts.append(f"Instagram post by @{self.author}")
        parts.append("\nPost caption:\n" + self.caption)
        return "\n".join(parts)

    def to_json(self) -> str:
        return json.dumps(
            {
                "shortcode": self.shortcode,
                "caption": self.caption,
                "author": self.author,
                "thumbnail_url": self.thumbnail_url,
            }
        )


def embed_url(shortcode: str) -> str:
    """The public embed page for a shortcode. ``/p/`` serves reels and photo posts alike."""
    return f"https://www.instagram.com/p/{shortcode}/embed/captioned/"


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _strip_trailing_hashtags(caption: str) -> str:
    """Drop the hashtag block creators pile at the end; it is pure noise in an LLM prompt.

    Only *trailing* hashtag-only lines go -- an inline "#ad" mid-sentence is left alone, and a
    caption that is nothing but hashtags is returned unchanged rather than emptied.
    """
    lines = caption.splitlines()
    kept = list(lines)
    while kept:
        words = kept[-1].split()
        if words and all(word.startswith("#") for word in words):
            kept.pop()
        else:
            break
    return "\n".join(kept).strip() or caption.strip()


def _caption_from_context_json(html: str) -> str | None:
    """Pull the caption out of the embed page's inline ``contextJSON`` blob.

    The blob is a JS string literal holding JSON, so it is double-escaped; decoding it as a JSON
    string first turns ``\\\\u0040`` and friends back into real characters.
    """
    match = re.search(r'"contextJSON"\s*:\s*("(?:[^"\\]|\\.)*")', html)
    if not match:
        return None
    try:
        inner = json.loads(match.group(1))
        payload = json.loads(inner)
    except (json.JSONDecodeError, ValueError):
        return None
    media = payload.get("graphql", {}).get("shortcode_media") or payload.get("shortcode_media")
    if not isinstance(media, dict):
        return None
    edges = (media.get("edge_media_to_caption") or {}).get("edges") or []
    for edge in edges:
        text = _clean((edge.get("node") or {}).get("text"))
        if text:
            return text
    return None


def _caption_from_markup(soup: Any) -> str | None:
    """Read the rendered ``.Caption`` block, dropping the username link and the comment tail."""
    block = soup.select_one(".Caption")
    if block is None:
        return None
    for junk in block.select(".CaptionUsername, .CaptionComments"):
        junk.decompose()
    return _clean(block.get_text("\n", strip=True))


def _caption_from_og_description(soup: Any) -> str | None:
    """Last resort: og:description. Instagram truncates it, so it is genuinely worse than above.

    The tag reads like ``12 likes, 3 comments - user on July 4, 2026: "caption"``; the quoted tail
    is the caption.
    """
    tag = soup.find("meta", attrs={"property": "og:description"})
    raw = _clean(tag.get("content")) if tag is not None else None
    if not raw:
        return None
    quoted = re.search(r'[:\-]\s*"(.+)"\s*$', raw, re.S)
    return _clean(quoted.group(1)) if quoted else raw


def parse_embed_html(html: str, shortcode: str) -> InstagramData:
    """Extract caption, author, and thumbnail from an embed page. Raises on an unreadable post.

    Tries the inline JSON first (complete and unescaped), then the rendered markup, then
    og:description (truncated). Every strategy is defensive because none of this is a contract
    Instagram offers us.
    """
    from bs4 import BeautifulSoup  # lazy: bs4 comes with recipe-scrapers (CONVENTIONS 4)

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    if any(marker.lower() in text.lower() for marker in _UNAVAILABLE_MARKERS):
        raise InstagramError(
            "Instagram would not show that post to a logged-out viewer - it may be private or "
            "removed. Open it in Safari and share it with the HTML-capture shortcut instead."
        )

    caption = (
        _caption_from_context_json(html)
        or _caption_from_markup(soup)
        or _caption_from_og_description(soup)
    )
    if not caption:
        raise InstagramError(
            "No caption was found on that Instagram post - if it is a video with the recipe only "
            "spoken aloud, there is nothing to import."
        )

    author_tag = soup.select_one(".CaptionUsername")
    author = _clean(author_tag.get_text(strip=True)) if author_tag is not None else None
    if author is None:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        author = _clean(og_title.get("content")) if og_title is not None else None
    image_tag = soup.find("meta", attrs={"property": "og:image"})
    thumbnail = _clean(image_tag.get("content")) if image_tag is not None else None
    if thumbnail is None:
        img = soup.select_one("img.EmbeddedMediaImage")
        thumbnail = _clean(img.get("src")) if img is not None else None

    return InstagramData(
        shortcode=shortcode,
        caption=_strip_trailing_hashtags(caption)[:_MAX_CAPTION_CHARS],
        author=(author or "").lstrip("@") or None,
        thumbnail_url=thumbnail,
    )


def fetch(shortcode: str) -> InstagramData:
    """Fetch a public post's caption via the embed page. Raises InstagramError on any failure."""
    try:
        result = fetch_service.fetch(embed_url(shortcode))
    except fetch_service.FetchError as exc:
        raise InstagramError(f"could not reach Instagram: {exc.message}") from exc
    return parse_embed_html(result.html, shortcode)
