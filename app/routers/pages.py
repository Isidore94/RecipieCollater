"""Primary navigation shell and Phase-0 empty tab screens.

Every tab requires a paired device. The tabs render deliberately empty "coming in a
later phase" states — no recipe/pantry/plan/chat features exist yet.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import current_user
from app.deps import get_db
from app.services import recipes as recipe_service
from app.services.users import User
from app.templating import render

router = APIRouter()

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


def _render_library(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    status: str,
    title: str,
    query: str | None,
) -> Response:
    return render(
        request,
        "recipes/browse.html",
        active_nav=status if status in ("inbox", "cookbook") else None,
        user=user,
        tab_title=title,
        status=status,
        query=query or "",
        recipes=recipe_service.list_recipes(db, status=status, query=query),
    )


@router.get("/inbox")
def inbox(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render_library(request, db, user, "inbox", "Test Recipes", q)


@router.get("/cookbook")
def cookbook(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render_library(request, db, user, "cookbook", "Cookbook", q)


@router.get("/archive")
def archive(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render_library(request, db, user, "archived", "Archive", q)


@router.get("/pantry")
def pantry(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "pantry")


@router.get("/plan")
def plan(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "plan")


@router.get("/chat")
def chat(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "chat")
