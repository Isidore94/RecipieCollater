"""Shopping list UI + export (Phase 4d). Thin: parse form -> shopping.py -> redirect / plain export.

Cookie-auth browser routes; every POST is CSRF-guarded. The list works with JavaScript off (plain
POST forms). Export endpoints are GETs so the phone's Share sheet or an Apple Shortcut can fetch it.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import recipes, shopping
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/shopping")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


@router.get("")
def index(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    list_id = shopping.active_list(db)
    remaining, total = shopping.counts(db, list_id)
    return render(
        request, "shopping/index.html", active_nav="pantry", user=user,
        aisles=shopping.grouped(db, list_id), remaining=remaining, total=total,
        reminders_text=shopping.to_reminders_text(db, list_id),
    )


@router.post("/add")
async def add_manual(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        with contextlib.suppress(shopping.ShoppingError):
            shopping.add_manual(db, shopping.active_list(db), _str(form, "text"))
    return RedirectResponse("/shopping", status_code=303)


@router.post("/add-staples")
async def add_staples(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    shopping.add_staples(db, shopping.active_list(db))
    return RedirectResponse("/shopping", status_code=303)


@router.post("/from-recipe/{slug}")
async def add_from_recipe(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/cookbook", status_code=303)
    async with request.form() as form:
        servings = _str(form, "servings") or None
        with contextlib.suppress(shopping.ShoppingError):
            shopping.add_from_recipe(
                db, shopping.active_list(db), detail.id, servings=servings, missing_only=True
            )
    return RedirectResponse("/shopping", status_code=303)


@router.post("/items/{item_id}/toggle")
async def toggle(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    with contextlib.suppress(shopping.ShoppingError):
        shopping.toggle(db, item_id)
    return RedirectResponse("/shopping", status_code=303)


@router.post("/items/{item_id}/remove")
async def remove(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    shopping.remove(db, item_id)
    return RedirectResponse("/shopping", status_code=303)


@router.post("/clear-checked")
async def clear_checked(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    shopping.clear_checked(db, shopping.active_list(db))
    return RedirectResponse("/shopping", status_code=303)


@router.get("/export.txt")
def export_text(
    db: sqlite3.Connection = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    return PlainTextResponse(shopping.to_text(db, shopping.active_list(db)))


@router.get("/export.json")
def export_json(
    db: sqlite3.Connection = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    body = json.dumps(shopping.to_json(db, shopping.active_list(db)))
    return Response(content=body, media_type="application/json")
