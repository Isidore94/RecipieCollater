"""Optional PIN sign-in — an additional way to mint a device session (CONVENTIONS §5).

A correct name + PIN creates a new rc_session device session, exactly like consuming a
pairing code; the cookie stays the one identity authority. PIN hashing and per-user rate
limiting live in app.services.credentials.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import clear_session_cookie, current_user, require_csrf, set_session_cookie
from app.deps import get_db
from app.security import SESSION_COOKIE_NAME
from app.services import credentials, sessions
from app.services.users import User, get_user
from app.templating import render

router = APIRouter()


def _device_name(request: Request) -> str:
    ua = request.headers.get("user-agent", "").lower()
    for needle, label in (
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Phone"),
        ("macintosh", "Mac"),
        ("windows", "PC"),
    ):
        if needle in ua:
            return label
    return "device"


def _login_page(
    request: Request, db: sqlite3.Connection, *, error: str = "", status: int = 200
) -> Response:
    return render(
        request,
        "onboarding/login.html",
        names=credentials.names_with_pin(db),
        error=error or None,
        status_code=status,
    )


@router.get("/login")
def login_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _login_page(request, db)


@router.post("/login")
def login(
    request: Request,
    name: str = Form(...),
    pin: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_csrf),
) -> Response:
    result = credentials.check_login(db, name, pin)
    if result.locked:
        return _login_page(
            request,
            db,
            error="Too many incorrect attempts. Wait a few minutes and try again.",
            status=429,
        )
    if not result.ok or result.user_id is None:
        return _login_page(request, db, error="That name and PIN do not match.", status=401)
    user = get_user(db, result.user_id)
    if user is None:  # pragma: no cover - check_login already resolved this row
        return _login_page(request, db, error="That name and PIN do not match.", status=401)
    raw = sessions.create_session(db, user.id, _device_name(request))
    redirect = RedirectResponse(url="/", status_code=303)
    set_session_cookie(redirect, raw)
    return redirect


@router.post("/logout")
def logout(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Sign this device out.

    There was no way out of a session at all: if her phone ended up on someone else's account
    the only recovery was clearing Safari's data. Revoking server-side as well as dropping the
    cookie keeps the Devices page honest about what is still signed in.
    """
    sessions.revoke_by_token(db, request.cookies.get(SESSION_COOKIE_NAME, ""))
    redirect = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(redirect)
    return redirect
