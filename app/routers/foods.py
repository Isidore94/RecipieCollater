"""Food upkeep screen (Phase 4.6). Thin: parse form -> services.foods -> redirect back.

Cookie-auth browser routes; every POST is CSRF-guarded. This is where the household reviews
pending imported foods, merges duplicates, sets aisles, pack sizes, and food families - the
hygiene that keeps matching/deductions/shopping truthful.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.routers import flash
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


def _back_url(form: FormData) -> str:
    query = _str(form, "q")
    return f"/foods?q={quote(query)}" if query else "/foods"


def _back(form: FormData, *, notice: str | None = None, error: str | None = None) -> Response:
    return flash.redirect(_back_url(form), notice=notice, error=error)


@router.get("")
def index(
    request: Request,
    q: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    food_list = foods.list_foods(db, query=q)
    return render(
        request, "foods/index.html", active_nav="pantry", user=user,
        foods=food_list, query=q or "", notice=notice, error=error,
        undo=undo, undo_kind=undo_kind,
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
        try:
            foods.confirm_food(db, food_id)
        except foods.FoodError as exc:
            return _back(form, error=str(exc))
        return _back(form, notice="Confirmed.")


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
        if target_id is None:
            return _back(form, error="Pick which food to merge this one into.")
        try:
            merged = foods.merge_foods(db, food_id, target_id, merged_by=user.id)
        except foods.FoodError as exc:
            return _back(form, error=str(exc))
        return flash.redirect(
            _back_url(form),
            notice=f"Merged {merged.source_name} into {merged.target_name}.",
            undo=merged.merge_id, undo_kind="merge",
        )


@router.post("/merges/{merge_id}/undo")
async def undo_merge(
    request: Request,
    merge_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Split a merged food back out, moving back exactly the rows the merge moved."""
    async with request.form() as form:
        try:
            name = foods.undo_merge(db, merge_id)
        except foods.FoodError as exc:
            return _back(form, error=str(exc))
        return _back(form, notice=f"Split {name} back out.")


@router.post("/{food_id}/parent")
async def set_parent(
    request: Request,
    food_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        try:
            foods.set_parent(db, food_id, _int(form, "parent_id"))
        except foods.FoodError as exc:
            return _back(form, error=str(exc))
        return _back(form, notice="Saved.")


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
        try:
            foods.set_category(db, food_id, _str(form, "category") or None)
            foods.set_purchase(
                db, food_id, quantity_text=_str(form, "purchase_quantity") or None,
                unit=_str(form, "purchase_unit") or None,
                label=_str(form, "purchase_label") or None,
            )
        except (foods.FoodError, ValueError) as exc:
            return _back(form, error=str(exc))
        return _back(form, notice="Saved.")
