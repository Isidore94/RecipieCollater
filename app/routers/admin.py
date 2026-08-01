"""Admin: device & token management (docs/09 device onboarding; roadmap Phase 0).

Lists users, device sessions, and ingest tokens; mints onboarding links/codes and
scoped ingest tokens; revokes any of them. Freshly generated secrets are shown exactly
once by rendering the page directly (never via a redirect URL).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import require_admin, require_csrf
from app.config import get_settings
from app.deps import get_db
from app.services import admin_stats, credentials, onboarding, sessions, tokens
from app.services.users import User, create_user, get_user, get_user_by_name, list_users
from app.templating import render

router = APIRouter(prefix="/admin")


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    return render(
        request, "admin/dashboard.html", active_nav=None, user=admin,
        stats=admin_stats.gather(db, get_settings()),
    )


def _devices_context(db: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    return {
        "users": list_users(db),
        "sessions": sessions.list_sessions(db),
        "tokens": tokens.list_tokens(db),
        "pin_user_ids": credentials.user_ids_with_pin(db),
        "app_base_url": get_settings().app_base_url,
        **extra,
    }


def _render_devices(
    request: Request, db: sqlite3.Connection, admin: User, **extra: Any
) -> Response:
    return render(
        request,
        "admin/devices.html",
        active_nav=None,
        user=admin,
        **_devices_context(db, **extra),
    )


@router.get("/devices")
def devices(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    return _render_devices(request, db, admin)


@router.post("/users")
def add_user(
    request: Request,
    name: str = Form(...),
    is_admin: bool = Form(False),
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    name = name.strip()
    if not name:
        return _render_devices(request, db, admin, error="Enter a name.")
    if get_user_by_name(db, name) is not None:
        return _render_devices(request, db, admin, error=f"User “{name}” already exists.")
    create_user(db, name, is_admin=is_admin)
    return _render_devices(request, db, admin, notice=f"Added {name}.")


@router.post("/devices/invite")
def invite_device(
    request: Request,
    user_id: int = Form(...),
    device_name: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    target = get_user(db, user_id)
    if target is None:
        return _render_devices(request, db, admin, error="Unknown user.")
    device = device_name.strip() or None
    link = onboarding.issue_magic_link(db, target.id, device)
    code = onboarding.issue_pairing_code(db, target.id, device)
    invite = {
        "user": target,
        "device_name": device,
        "magic_link_url": f"{get_settings().app_base_url}/pair?t={link.raw}",
        "pairing_code": code.raw,
        "expires_at": link.expires_at,
    }
    return _render_devices(request, db, admin, invite=invite)


@router.post("/devices/token")
def create_token(
    request: Request,
    user_id: int = Form(...),
    label: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    target = get_user(db, user_id)
    if target is None:
        return _render_devices(request, db, admin, error="Unknown user.")
    raw = tokens.create_ingest_token(db, target.id, label.strip() or "ingest token")
    new_token = {"user": target, "label": label.strip(), "raw": raw}
    return _render_devices(request, db, admin, new_token=new_token)


@router.post("/devices/session/{session_id}/revoke")
def revoke_session(
    request: Request,
    session_id: int,
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    sessions.revoke_session(db, session_id)
    return RedirectResponse(url="/admin/devices", status_code=303)


@router.post("/devices/token/{token_id}/revoke")
def revoke_token(
    request: Request,
    token_id: int,
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    tokens.revoke_token(db, token_id)
    return RedirectResponse(url="/admin/devices", status_code=303)


@router.post("/users/{user_id}/pin")
def set_user_pin(
    request: Request,
    user_id: int,
    pin: str = Form(...),
    pin_confirm: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    target = get_user(db, user_id)
    if target is None:
        return _render_devices(request, db, admin, error="Unknown user.")
    # A PIN is write-only and typed blind, so a slip locks the person out of the app with no
    # way to discover it. Confirming is the only check available.
    if pin_confirm and pin != pin_confirm:
        return _render_devices(
            request, db, admin,
            error=f"{target.name}: the two PINs do not match. Nothing was changed.",
        )
    try:
        credentials.set_pin(db, target.id, pin)
    except credentials.PinError as exc:
        return _render_devices(request, db, admin, error=f"{target.name}: {exc}")
    return _render_devices(request, db, admin, notice=f"PIN set for {target.name}.")


@router.post("/users/{user_id}/pin/clear")
def clear_user_pin(
    request: Request,
    user_id: int,
    db: sqlite3.Connection = Depends(get_db),
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
) -> Response:
    target = get_user(db, user_id)
    if target is None:
        return _render_devices(request, db, admin, error="Unknown user.")
    credentials.clear_pin(db, target.id)
    return _render_devices(request, db, admin, notice=f"PIN removed for {target.name}.")
