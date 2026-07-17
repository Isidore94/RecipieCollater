"""Primary navigation shell and Phase-0 empty tab screens.

Every tab requires a paired device. The tabs render deliberately empty "coming in a
later phase" states — no recipe/pantry/plan/chat features exist yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import current_user, require_csrf
from app.services.users import User
from app.templating import render

router = APIRouter()

_VALID_THEMES = {"auto", "light", "dark"}

_EMPTY_TABS = {
    "inbox": ("Test Recipes", "Shared recipes will land here once ingestion is built."),
    "cookbook": ("Cookbook", "Your saved recipes will live here."),
    "pantry": ("Pantry", "Track what's on hand once the pantry is built."),
    "plan": ("Plan", "Weekly meal plans and shopping lists arrive later."),
    "chat": ("Assistant", "The AI assistant arrives once the cookbook and pantry exist."),
}


@router.get("/")
def home(user: User = Depends(current_user)) -> Response:
    return RedirectResponse(url="/inbox", status_code=307)


def _render_tab(request: Request, user: User, key: str) -> Response:
    title, blurb = _EMPTY_TABS[key]
    return render(
        request,
        "tab_empty.html",
        active_nav=key,
        user=user,
        tab_title=title,
        tab_blurb=blurb,
    )


@router.get("/inbox")
def inbox(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "inbox")


@router.get("/cookbook")
def cookbook(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "cookbook")


@router.get("/pantry")
def pantry(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "pantry")


@router.get("/plan")
def plan(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "plan")


@router.get("/chat")
def chat(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "chat")


@router.post("/theme")
def set_theme(
    request: Request,
    theme: str = Form(...),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    choice = theme if theme in _VALID_THEMES else "auto"
    referer = request.headers.get("referer", "/inbox")
    response = RedirectResponse(url=referer, status_code=303)
    # Per-device preference, distinct from the auth cookie. One year is fine.
    response.set_cookie(
        "rc_theme", choice, max_age=31_536_000, httponly=False, samesite="lax", path="/"
    )
    return response
