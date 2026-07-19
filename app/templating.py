"""Jinja2 environment and a render helper that injects common context."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import PACKAGE_DIR, get_settings
from app.security import CSRF_HEADER
from app.services.users import User

_TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _asset_version() -> str:
    """A cache-busting token for the stylesheet, from its own mtime.

    iOS Safari (and a home-screen A2HS app especially) caches CSS aggressively, so a redeploy
    can keep serving the old stylesheet. Appending ?v=<mtime> to the href changes the URL every
    time the file changes, forcing a fresh fetch without any service worker. Frozen-aware: reads
    the bundled copy under PACKAGE_DIR.
    """
    try:
        return str(int((PACKAGE_DIR / "static" / "css" / "app.css").stat().st_mtime))
    except OSError:
        return "0"


ASSET_VERSION = _asset_version()


def safe_url(value: str | None) -> str:
    """Return the URL only if it is an http(s) link, else ''. Defence in depth for any value that
    reaches an href: HTML-escaping does not neutralise a 'javascript:' or 'data:' URI (#9.1)."""
    if not value:
        return ""
    trimmed = value.strip()
    return trimmed if trimmed.lower().startswith(("http://", "https://")) else ""


_TEMPLATES.env.filters["safe_url"] = safe_url

# Primary navigation (docs/07-ui-ux.md §2). Phase-5 full set. Shopping keeps its own tab
# (the household's most-used away-from-home surface) rather than folding under Plan, so the tab
# bar carries six destinations; Inbox stays reachable via the Cookbook library-nav and Home's
# "new to try". Plan owns the week board; Chat is the assistant.
NAV_ITEMS: list[dict[str, str]] = [
    {"key": "home", "label": "Home", "href": "/", "icon": "home"},
    {"key": "cookbook", "label": "Cookbook", "href": "/cookbook", "icon": "book"},
    {"key": "pantry", "label": "Pantry", "href": "/pantry", "icon": "box"},
    {"key": "shopping", "label": "Shopping", "href": "/shopping", "icon": "cart"},
    {"key": "plan", "label": "Plan", "href": "/plan", "icon": "calendar"},
    {"key": "chat", "label": "Chat", "href": "/chat", "icon": "chat"},
]


def render(
    request: Request,
    template_name: str,
    *,
    active_nav: str | None = None,
    user: User | None = None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    settings = get_settings()
    base = {
        "request": request,
        "app_base_url": settings.app_base_url,
        "nav_items": NAV_ITEMS,
        "active_nav": active_nav,
        "user": user,
        "csrf_header": CSRF_HEADER,
        "asset_version": ASSET_VERSION,
        **context,
    }
    return _TEMPLATES.TemplateResponse(
        request=request, name=template_name, context=base, status_code=status_code
    )
