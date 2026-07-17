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
    """Reject cross-site state-changing requests.

    ``SameSite=Lax`` on ``rc_session`` is the primary defense: the browser withholds the auth
    cookie from every cross-site POST, so a forged cross-site request carries no authority. On
    top of that we reject any request the browser *explicitly* labels ``Sec-Fetch-Site:
    cross-site``.

    We deliberately do NOT require Fetch Metadata or a custom header. Measured 2026-07-17: iOS
    Safari on the plain-HTTP LAN omits ``Sec-Fetch-Site``, and a plain ``<form>`` post cannot add
    a custom header — so requiring either blocked legitimate onboarding / PIN sign-in from real
    family devices. Absent Fetch Metadata, ``SameSite=Lax`` is the guarantee (CONVENTIONS §7).
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
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
