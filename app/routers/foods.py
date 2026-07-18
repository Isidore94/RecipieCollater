"""Food upkeep screen (Phase 4.6). Thin: parse form -> services.foods -> redirect back.

Cookie-auth browser routes; every POST is CSRF-guarded. This is where the household reviews
pending imported foods, merges duplicates, sets aisles, pack sizes, and food families - the
hygiene that keeps matching/deductions/shopping truthful.
"""

from __future__ import annotations

import contextlib
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import foods
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/foods")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _int(form: FormData, key: str) -> int | None:
    raw = _str(form, key)
    return int(raw) if raw.isdigit() else None


def _back(form: FormData) -> Response:
    query = _str(form, "q")
    url = f"/foods?q={query}" if query else "/foods"
    return RedirectResponse(url, status_code=303)


@router.get("")
def index(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    food_list = foods.list_foods(db, query=q)
    return render(
        request, "foods/index.html", active_nav="pantry", user=user,
        foods=food_list, query=q or "",
        pending_count=sum(1 for f in food_list if f.status == "pending"),
    )


@router.post("/{food_id}/confirm")
async def confirm(
    request: Request,
    food_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        with contextlib.suppress(foods.FoodError):
            foods.confirm_food(db, food_id)
        return _back(form)


@router.post("/{food_id}/merge")
async def merge(
    request: Request,
    food_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        target_id = _int(form, "target_id")
        if target_id is not None:
            with contextlib.suppress(foods.FoodError):
                foods.merge_foods(db, food_id, target_id)
        return _back(form)


@router.post("/{food_id}/parent")
async def set_parent(
    request: Request,
    food_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        with contextlib.suppress(foods.FoodError):
            foods.set_parent(db, food_id, _int(form, "parent_id"))
        return _back(form)


@router.post("/{food_id}/details")
async def set_details(
    request: Request,
    food_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Aisle + purchase info in one save (they share the per-food editor row)."""
    async with request.form() as form:
        with contextlib.suppress(foods.FoodError, ValueError):
            foods.set_category(db, food_id, _str(form, "category") or None)
            foods.set_purchase(
                db, food_id, quantity_text=_str(form, "purchase_quantity") or None,
                unit=_str(form, "purchase_unit") or None,
                label=_str(form, "purchase_label") or None,
            )
        return _back(form)
