"""Vendored front-end libraries must match the checksums recorded in VENDOR.md
(CONVENTIONS §8: self-hosted, pinned, no runtime CDN)."""

from __future__ import annotations

import hashlib
import re

from app.config import PACKAGE_DIR

VENDOR_DIR = PACKAGE_DIR / "static" / "vendor"


def _recorded_checksums() -> dict[str, str]:
    text = (VENDOR_DIR / "VENDOR.md").read_text(encoding="utf-8")
    # Rows look like: | `htmx.min.js` | htmx | 2.0.4 | `<sha>` |
    pattern = re.compile(r"\|\s*`([^`]+\.js)`\s*\|[^|]*\|[^|]*\|\s*`([0-9a-f]{64})`")
    return {m.group(1): m.group(2) for m in pattern.finditer(text)}


def test_vendored_files_match_recorded_checksums() -> None:
    recorded = _recorded_checksums()
    assert recorded, "VENDOR.md must record at least one checksum"
    for filename, expected in recorded.items():
        path = VENDOR_DIR / filename
        assert path.exists(), f"missing vendored file: {filename}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, f"{filename} checksum drift vs VENDOR.md"


def test_expected_vendor_files_present() -> None:
    for name in ("htmx.min.js", "alpine.min.js"):
        assert (VENDOR_DIR / name).exists()
