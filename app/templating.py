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


def safe_url(value: str | None) -> str:
    """Return the URL only if it is an http(s) link, else ''. Defence in depth for any value that
    reaches an href: HTML-escaping does not neutralise a 'javascript:' or 'data:' URI (#9.1)."""
    if not value:
        return ""
    trimmed = value.strip()
    return trimmed if trimmed.lower().startswith(("http://", "https://")) else ""


_TEMPLATES.env.filters["safe_url"] = safe_url

# Primary navigation (docs/07-ui-ux.md §2). Phase-0 tabs render empty states.
NAV_ITEMS: list[dict[str, str]] = [
    {"key": "inbox", "label": "Inbox", "href": "/inbox", "icon": "inbox"},
    {"key": "cookbook", "label": "Cookbook", "href": "/cookbook", "icon": "book"},
    {"key": "pantry", "label": "Pantry", "href": "/pantry", "icon": "box"},
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
        **context,
    }
    return _TEMPLATES.TemplateResponse(
        request=request, name=template_name, context=base, status_code=status_code
    )
