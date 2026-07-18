"""Pantry UI (Phase 4b). Thin: parse form -> pantry.py -> redirect back to the pantry.

Cookie-auth browser routes only; every POST is CSRF-guarded (SameSite + require_csrf). Pantry
state is server-authoritative; the page is plain forms so a stock-take works with JavaScript off.
"""

from __future__ import annotations

import contextlib
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import pantry
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/pantry")

# Adjustment reasons a stock-take/manual tap may assert (never the cook/remove-only reasons).
_ADJUST_REASONS = frozenset({"correction", "stock_take", "restock"})


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _int(form: FormData, key: str) -> int | None:
    raw = form.get(key)
    if isinstance(raw, str) and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _checked(form: FormData, key: str) -> bool:
    return _str(form, key) in ("on", "true", "1", "yes")


def _reason(form: FormData) -> str:
    reason = _str(form, "reason")
    return reason if reason in _ADJUST_REASONS else "correction"


def _back(form: FormData) -> str:
    """Where to return after a mutation - preserves the location tab / stock-take view."""
    target = _str(form, "next")
    return target if target.startswith("/pantry") else "/pantry"


@router.get("")
def index(
    request: Request,
    location: int | None = None,
    q: str | None = None,
    notice: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    locations = pantry.list_locations(db)
    active = location if any(loc.id == location for loc in locations) else None
    items = pantry.list_items(db, location_id=active, query=q)
    restock_count = len(pantry.shopping_candidates(db))
    return render(
        request, "pantry/index.html", active_nav="pantry", user=user,
        locations=locations, active_location=active, items=items, restock_count=restock_count,
        query=q or "", notice=notice,
    )


@router.get("/stock-take/{location_id}")
def stock_take(
    request: Request,
    location_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    location = next((loc for loc in pantry.list_locations(db) if loc.id == location_id), None)
    if location is None:
        return RedirectResponse("/pantry", status_code=303)
    items = pantry.list_items(db, location_id=location_id)
    return render(
        request, "pantry/stocktake.html", active_nav="pantry", user=user,
        location=location, items=items,
    )


@router.post("/locations")
async def add_location(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        name = _str(form, "name")
        if name:
            loc_id = pantry.create_location(db, name, is_freezer=_checked(form, "is_freezer"))
            return RedirectResponse(f"/pantry?location={loc_id}", status_code=303)
    return RedirectResponse("/pantry", status_code=303)


@router.post("/items")
async def add_item(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        location_id = _int(form, "location_id")
        back = _back(form)
        if location_id is None:
            return RedirectResponse(back, status_code=303)
        data = pantry.PantryItemInput(
            display_name=_str(form, "display_name"),
            location_id=location_id,
            quantity_mode=_str(form, "quantity_mode") or "gauge",
            food=_str(form, "food") or None,
            quantity_text=_str(form, "quantity_text") or None,
            unit=_str(form, "unit") or None,
            gauge=_str(form, "gauge") or None,
            is_staple=_checked(form, "is_staple"),
            min_quantity_text=_str(form, "min_quantity_text") or None,
            expires_on=_str(form, "expires_on") or None,
            step_down_on_cook=_checked(form, "step_down_on_cook"),
        )
        # A bad add just returns to the pantry; the form is forgiving by design.
        with contextlib.suppress(pantry.PantryError):
            pantry.add_item(db, data, user_id=user.id)
    return RedirectResponse(back, status_code=303)


@router.post("/items/{item_id}/adjust")
async def adjust_item(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        action = _str(form, "action")
        reason = _reason(form)
        with contextlib.suppress(pantry.PantryError):
            if action == "cycle":
                pantry.cycle_gauge(db, item_id, user_id=user.id)
            elif action == "gauge":
                pantry.set_gauge(db, item_id, _str(form, "gauge"), reason=reason, user_id=user.id)
            elif action == "step":
                pantry.step_exact(db, item_id, _str(form, "delta"), user_id=user.id)
            elif action == "set_exact":
                pantry.set_exact(db, item_id, _str(form, "quantity"), user_id=user.id)
            elif action == "toggle":
                pantry.toggle_have(db, item_id, user_id=user.id)
            elif action == "have":
                pantry.set_have(db, item_id, _checked(form, "have"), reason=reason, user_id=user.id)
        return RedirectResponse(_back(form), status_code=303)


@router.post("/items/{item_id}/staple")
async def set_staple(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        with contextlib.suppress(pantry.PantryError):
            pantry.set_staple(
                db, item_id, is_staple=_checked(form, "is_staple"),
                min_quantity_text=_str(form, "min_quantity_text") or None, user_id=user.id,
            )
        return RedirectResponse(_back(form), status_code=303)


@router.post("/items/{item_id}/expiry")
async def set_expiry(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        with contextlib.suppress(pantry.PantryError):
            pantry.set_expiry(db, item_id, _str(form, "expires_on") or None, user_id=user.id)
        return RedirectResponse(_back(form), status_code=303)


@router.post("/items/{item_id}/remove")
async def remove_item(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        reason = _str(form, "reason")
        reason = reason if reason in ("manual_remove", "spoiled") else "manual_remove"
        with contextlib.suppress(pantry.PantryError):
            pantry.remove_item(
                db, item_id, reason=reason, delete=_checked(form, "delete"), user_id=user.id
            )
        return RedirectResponse(_back(form), status_code=303)
