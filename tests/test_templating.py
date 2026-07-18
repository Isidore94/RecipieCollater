"""Template helpers: the safe_url filter that keeps script-bearing schemes out of hrefs (#9.1)."""

from __future__ import annotations

import pytest

from app.templating import safe_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/recipe", "https://example.com/recipe"),
        ("http://192.168.0.223:8765/x", "http://192.168.0.223:8765/x"),
        ("  https://example.com  ", "https://example.com"),
        ("javascript:alert(document.cookie)", ""),
        ("JavaScript:alert(1)", ""),
        ("data:text/html,<script>alert(1)</script>", ""),
        ("vbscript:msgbox(1)", ""),
        ("//evil.example.com", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_safe_url(value: str | None, expected: str) -> None:
    assert safe_url(value) == expected
