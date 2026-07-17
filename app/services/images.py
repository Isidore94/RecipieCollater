"""Post-extraction recipe images: SSRF-safe download, re-encoded to a bounded WEBP.

An extracted recipe carries an image URL (the page's og:image, or a YouTube thumbnail). We fetch
it with the same host-safety rules as the page fetcher, then re-encode with Pillow to a size- and
dimension-bounded WEBP under data/images/<recipe_id>/. Images are always optional: any failure
returns None and the recipe simply has no photo (CONVENTIONS: never fail an ingest over a picture).

The decode/encode step (:func:`encode_webp`) is separated from the network fetch so it can be
tested on in-memory bytes.
"""

from __future__ import annotations

import io
from urllib.parse import urlsplit

from app.config import get_settings

_MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
_MAX_DIMENSION = 1280
_WEBP_QUALITY = 82
_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def encode_webp(raw: bytes, recipe_id: int) -> str | None:
    """Decode image bytes and write a bounded WEBP; return its images-dir-relative path or None."""
    try:
        from PIL import Image  # lazy (CONVENTIONS 4)

        rgb = Image.open(io.BytesIO(raw)).convert("RGB")
        rgb.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))
        images_dir = get_settings().images_dir
        (images_dir / str(recipe_id)).mkdir(parents=True, exist_ok=True)
        relative = f"{recipe_id}/image.webp"
        rgb.save(images_dir / relative, "WEBP", quality=_WEBP_QUALITY)
        return relative
    except Exception:  # unreadable/corrupt image data - skip the photo, keep the recipe
        return None


def store_image_from_url(recipe_id: int, url: str) -> str | None:
    """Download an image URL (SSRF-checked, no redirects) and store as WEBP; None on failure."""
    from app.services import fetch  # local import avoids a fetch<->images cycle at module load

    host = urlsplit(url).hostname or ""
    if not host:
        return None
    try:
        fetch.assert_host_is_public(host)
    except fetch.FetchError:
        return None
    try:
        import httpx  # lazy (CONVENTIONS 4)

        resp = httpx.get(
            url, timeout=_TIMEOUT, follow_redirects=False, headers={"User-Agent": _USER_AGENT}
        )
        resp.raise_for_status()
        raw = resp.content
    except Exception:
        return None
    if not raw or len(raw) > _MAX_DOWNLOAD_BYTES:
        return None
    return encode_webp(raw, recipe_id)
