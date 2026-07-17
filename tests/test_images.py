"""Recipe images: WEBP re-encoding (offline, in-memory bytes) and the SSRF guard on downloads."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app.config import get_settings
from app.services import images


def _png_bytes(size: tuple[int, int] = (20, 20)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 80, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def test_encode_webp_writes_bounded_file(data_dir: Path) -> None:
    relative = images.encode_webp(_png_bytes((2000, 100)), 7)
    assert relative == "7/image.webp"
    out = get_settings().images_dir / relative
    assert out.is_file()
    with Image.open(out) as saved:
        assert saved.format == "WEBP"
        assert max(saved.size) <= 1280  # bounded to the max dimension


def test_encode_webp_rejects_garbage(data_dir: Path) -> None:
    assert images.encode_webp(b"not an image", 7) is None


def test_store_rejects_private_host(data_dir: Path) -> None:
    # 127.0.0.1 resolves to loopback locally, so the SSRF guard blocks it without any network.
    assert images.store_image_from_url(1, "http://127.0.0.1/photo.png") is None


def test_store_rejects_url_without_host(data_dir: Path) -> None:
    assert images.store_image_from_url(1, "not-a-url") is None
