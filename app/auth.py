"""Authentication dependencies and cookie helpers.

Two independent identity paths that never cross (CONVENTIONS §5, §6):
  * browser identity — the single ``rc_session`` cookie → a ``users`` row;
  * ingest identity — a scoped Bearer token → an ``api_tokens`` row.

A browser session must never authenticate the ingest API, and an ingest token must
never reach browser/app functions. Both boundaries are covered by tests.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.deps import get_db
from app.security import (
    CSRF_HEADER,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME,
)
from app.services import sessions, tokens
from app.services.users import User, get_user


class AuthRequired(Exception):
    """Raised when a browser route needs a valid session and none is present."""


class IngestAuthError(Exception):
    """Raised when the ingest API is called without a valid ingest token."""


class CSRFError(Exception):
    """Raised when a state-changing browser request lacks the custom header."""


# --------------------------------------------------------------------------------------
# Cookie helpers (exactly one persistent cookie)
# --------------------------------------------------------------------------------------


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,  # only over HTTPS upgrade; omitted on plain HTTP
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


# --------------------------------------------------------------------------------------
# Browser identity
# --------------------------------------------------------------------------------------


def current_user(
    request: Request,
    response: Response,
    db: sqlite3.Connection = Depends(get_db),
) -> User:
    """Require a valid device session. Applies sliding cookie renewal."""
    raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    resolved = sessions.resolve_session(db, raw)
    if resolved is None:
        raise AuthRequired
    user = get_user(db, resolved.session.user_id)
    if user is None:  # session referenced a deleted user
        raise AuthRequired
    if resolved.renewed:
        set_session_cookie(response, raw)
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise AuthRequired
    return user


# --------------------------------------------------------------------------------------
# Ingest identity (scoped Bearer token) — never satisfied by a browser cookie
# --------------------------------------------------------------------------------------


def require_ingest_token(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> tokens.ApiToken:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise IngestAuthError
    raw = header[len("bearer ") :].strip()
    token = tokens.resolve_token(db, raw, required_scope=tokens.INGEST_SCOPE)
    if token is None:
        raise IngestAuthError
    return token


# --------------------------------------------------------------------------------------
# CSRF — custom header a cross-site form cannot set (belt and braces atop SameSite=Lax)
# --------------------------------------------------------------------------------------


def require_csrf(request: Request) -> None:
    """Reject cross-site state-changing requests (belt-and-braces atop SameSite=Lax).

    Layered so both htmx and no-JS form fallback are protected:
      1. Fetch Metadata: if the browser sends ``Sec-Fetch-Site`` (all modern browsers,
         incl. iOS Safari), allow only ``same-origin``/``none`` — this header cannot be
         forged by a cross-site page and covers plain ``<form>`` posts.
      2. Legacy browsers without Fetch Metadata must carry a custom header a cross-site
         HTML form cannot set (htmx's ``HX-Request`` or the app's ``X-RC-CSRF``).
    ``SameSite=Lax`` already withholds the auth cookie from cross-site POSTs underneath.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site in {"same-origin", "none"}:
            return
        raise CSRFError
    if "hx-request" in request.headers or CSRF_HEADER in request.headers:
        return
    raise CSRFError


# --------------------------------------------------------------------------------------
# Exception handlers (registered in app.main)
# --------------------------------------------------------------------------------------


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


async def auth_required_handler(request: Request, exc: Exception) -> Response:
    if _is_api(request):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    # Browser navigation → send to the onboarding/welcome page.
    return RedirectResponse(url="/welcome", status_code=303)


async def ingest_auth_handler(request: Request, exc: Exception) -> Response:
    return JSONResponse({"detail": "invalid or missing ingest token"}, status_code=401)


async def csrf_handler(request: Request, exc: Exception) -> Response:
    return JSONResponse({"detail": "missing CSRF header"}, status_code=403)
