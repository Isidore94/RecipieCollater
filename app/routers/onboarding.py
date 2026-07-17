"""Device onboarding: first-run admin bootstrap, magic-link, and pairing-code entry.

Flows (docs/09-… device onboarding, docs/07-ui-ux.md):
  * First run (no users): a one-time setup form creates the first admin + this device.
  * Magic link:  GET /pair?t=<token> consumes a single-use link and pairs this device.
  * Pairing code: a cookie-less standalone launch types the 6-char code from the admin
    Devices page.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import require_csrf, set_session_cookie
from app.deps import get_db
from app.services import onboarding, sessions
from app.services.users import count_users, create_user, get_user
from app.templating import render

router = APIRouter()


def _default_device_name(request: Request, fallback: str = "device") -> str:
    ua = request.headers.get("user-agent", "")
    if "iphone" in ua.lower():
        return "iPhone"
    if "ipad" in ua.lower():
        return "iPad"
    if "macintosh" in ua.lower():
        return "Mac"
    if "windows" in ua.lower():
        return "PC"
    return fallback


@router.get("/welcome")
def welcome(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    if count_users(db) == 0:
        return render(request, "onboarding/setup.html")
    return render(request, "onboarding/welcome.html")


@router.post("/setup")
def first_run_setup(
    request: Request,
    response: Response,
    admin_name: str = Form(...),
    device_name: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_csrf),
) -> Response:
    # First-run only. Once any user exists this endpoint refuses (no privilege escalation).
    if count_users(db) != 0:
        return RedirectResponse(url="/welcome", status_code=303)
    name = admin_name.strip()
    if not name:
        return render(
            request, "onboarding/setup.html", error="Please enter your name.", status_code=400
        )
    user = create_user(db, name, is_admin=True)
    raw = sessions.create_session(db, user.id, device_name.strip() or _default_device_name(request))
    redirect = RedirectResponse(url="/", status_code=303)
    set_session_cookie(redirect, raw)
    return redirect


@router.get("/pair")
def pair_via_magic_link(
    request: Request,
    t: str = "",
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    consumed = onboarding.consume(db, t, kind="magic_link")
    if consumed is None:
        return render(
            request,
            "onboarding/welcome.html",
            error="That pairing link is invalid or has expired. Ask an admin for a new one.",
            status_code=400,
        )
    user = get_user(db, consumed.user_id)
    if user is None:
        return render(request, "onboarding/welcome.html", error="Unknown user.", status_code=400)
    raw = sessions.create_session(
        db, user.id, consumed.device_name or _default_device_name(request)
    )
    redirect = RedirectResponse(url="/", status_code=303)
    set_session_cookie(redirect, raw)
    return redirect


@router.post("/pair/code")
def pair_via_code(
    request: Request,
    code: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_csrf),
) -> Response:
    consumed = onboarding.consume(db, code, kind="pairing_code")
    if consumed is None:
        return render(
            request,
            "onboarding/welcome.html",
            error="That code is invalid or has expired. Ask an admin for a fresh code.",
            status_code=400,
        )
    user = get_user(db, consumed.user_id)
    if user is None:
        return render(request, "onboarding/welcome.html", error="Unknown user.", status_code=400)
    raw = sessions.create_session(
        db, user.id, consumed.device_name or _default_device_name(request)
    )
    redirect = RedirectResponse(url="/", status_code=303)
    set_session_cookie(redirect, raw)
    return redirect
