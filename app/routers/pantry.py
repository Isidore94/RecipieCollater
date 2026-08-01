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
from app.routers import flash
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


def _location_from(back: str) -> int | None:
    """The location tab the card was rendered under, so an htmx re-render matches the page.

    Only the location-filtered view hides the per-card location chip; recovering it from the
    return URL keeps the swapped card identical to the one it replaces.
    """
    _, _, query = back.partition("?")
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == "location" and value.isdigit():
            return int(value)
    return None


@router.get("")
def index(
    request: Request,
    location: int | None = None,
    q: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
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
        query=q or "", notice=notice, error=error, undo=undo,
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
        if not name:
            return flash.redirect("/pantry", error="Give the location a name first.")
        try:
            loc_id = pantry.create_location(db, name, is_freezer=_checked(form, "is_freezer"))
        except pantry.PantryError as exc:
            return flash.redirect("/pantry", error=str(exc))
    return flash.redirect(f"/pantry?location={loc_id}", notice=f"Added {name}.")


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
            return flash.redirect(
                back,
                error="Add a location first (a cupboard, the freezer\u2026), "
                      "then add items to it.",
            )
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
        try:
            pantry.add_item(db, data, user_id=user.id)
        except pantry.PantryError as exc:
            return flash.redirect(back, error=str(exc))
    return flash.redirect(back, notice=f"Added {data.display_name}.")


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
        adjustment_id: int | None = None
        with contextlib.suppress(pantry.PantryError):
            if action == "cycle":
                pantry.cycle_gauge(db, item_id, user_id=user.id)
            elif action == "gauge":
                adjustment_id = pantry.set_gauge(
                    db, item_id, _str(form, "gauge"), reason=reason, user_id=user.id
                )
            elif action == "step":
                adjustment_id = pantry.step_exact(db, item_id, _str(form, "delta"), user_id=user.id)
            elif action == "set_exact":
                adjustment_id = pantry.set_exact(
                    db, item_id, _str(form, "quantity"), user_id=user.id
                )
            elif action == "toggle":
                pantry.toggle_have(db, item_id, user_id=user.id)
            elif action == "have":
                adjustment_id = pantry.set_have(
                    db, item_id, _checked(form, "have"), reason=reason, user_id=user.id
                )
        if request.headers.get("HX-Request"):
            # Swap just this card: a shelf stock-take is twenty taps, not twenty page loads.
            item = pantry.get_item(db, item_id)
            if item is None:
                return Response("", status_code=200)
            back = _back(form)
            return render(
                request, "pantry/_item.html", user=user, item=item, back=back,
                active_location=_location_from(back),
            )
        # Without JavaScript there is no swapped card to re-tap, so the redirect carries the undo.
        return flash.redirect(_back(form), notice="Updated.", undo=adjustment_id)


@router.post("/items/{item_id}/tracking")
async def set_tracking(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Change how an item is tracked (a mis-guessed avocado should not need deleting)."""
    async with request.form() as form:
        back = _back(form)
        mode = _str(form, "quantity_mode")
        try:
            pantry.set_quantity_mode(db, item_id, mode, user_id=user.id)
        except pantry.PantryError as exc:
            return flash.redirect(back, error=str(exc))
    labels = {"exact": "counted", "gauge": "a rough level", "binary": "have or out"}
    return flash.redirect(back, notice=f"Now tracked as {labels.get(mode, mode)}.")


@router.post("/adjustments/{adjustment_id}/undo")
async def undo_adjustment(
    request: Request,
    adjustment_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Reverse the change the banner is offering to take back."""
    async with request.form() as form:
        back = _back(form)
        try:
            name = pantry.undo_adjustment(db, adjustment_id, user_id=user.id)
        except pantry.PantryError as exc:
            return flash.redirect(back, error=str(exc))
    return flash.redirect(back, notice=f"Put {name} back.")


@router.post("/items/{item_id}/staple")
async def set_staple(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        back = _back(form)
        try:
            pantry.set_staple(
                db, item_id, is_staple=_checked(form, "is_staple"),
                min_quantity_text=_str(form, "min_quantity_text") or None, user_id=user.id,
            )
        except pantry.PantryError as exc:
            return flash.redirect(back, error=str(exc))
        return flash.redirect(back, notice="Saved.")


@router.post("/items/{item_id}/expiry")
async def set_expiry(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        back = _back(form)
        try:
            pantry.set_expiry(db, item_id, _str(form, "expires_on") or None, user_id=user.id)
        except pantry.PantryError as exc:
            return flash.redirect(back, error=str(exc))
        return flash.redirect(back, notice="Saved.")


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
        back = _back(form)
        deleted = _checked(form, "delete")
        try:
            adjustment_id = pantry.remove_item(
                db, item_id, reason=reason, delete=deleted, user_id=user.id
            )
        except pantry.PantryError as exc:
            return flash.redirect(back, error=str(exc))
        if deleted:
            done = "Deleted."
        else:
            done = "Marked as gone bad." if reason == "spoiled" else "Marked as used up."
        return flash.redirect(back, notice=done, undo=adjustment_id)
