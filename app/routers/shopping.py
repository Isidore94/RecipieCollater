"""Shopping list UI + export (Phase 4d/4.6). Thin: parse form -> shopping.py -> redirect/render.

Cookie-auth browser routes; every POST is CSRF-guarded. The list works with JavaScript off (plain
POST forms). Export endpoints are GETs so the phone's Share sheet or an Apple Shortcut can fetch it.
The trip builder and restock review both recompute server-side on apply, so a stale form can't
write decisions the pantry no longer supports.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.routers import flash
from app.services import pantry, recipes, shopping
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/shopping")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _notice_redirect(notice: str | None = None, error: str | None = None) -> Response:
    return flash.redirect("/shopping", notice=notice, error=error)


def _picks_from_form(form: FormData) -> list[tuple[int, str | None]]:
    """(recipe_id, servings) pairs from the plan/preview/apply forms."""
    picks: list[tuple[int, str | None]] = []
    for raw in form.getlist("recipe"):
        if not (isinstance(raw, str) and raw.isdigit()):
            continue
        recipe_id = int(raw)
        servings = _str(form, f"servings_{recipe_id}") or None
        picks.append((recipe_id, servings))
    return picks


@router.get("")
def index(
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    list_id = shopping.active_list(db)
    remaining, total = shopping.counts(db, list_id)
    return render(
        request, "shopping/index.html", active_nav="shopping", user=user,
        aisles=shopping.grouped(db, list_id), remaining=remaining, total=total,
        reminders_text=shopping.to_reminders_text(db, list_id),
        sources=shopping.sources_by_item(db, list_id), notice=notice, error=error,
    )


@router.get("/badge")
def badge(
    db: sqlite3.Connection = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    """htmx fragment: the unchecked-items count for the nav tab (empty when zero).

    The slot re-arms its own listener on every swap so an inline check-off elsewhere on the
    page can refresh the badge by firing ``rc:shopping-changed``.
    """
    remaining, _total = shopping.counts(db, shopping.active_list(db))
    inner = f'<span class="tab-badge">{remaining}</span>' if remaining else ""
    return HTMLResponse(
        '<span class="tab-badge-slot" hx-get="/shopping/badge" '
        'hx-trigger="rc:shopping-changed from:body" hx-swap="outerHTML">'
        f"{inner}</span>"
    )


@router.post("/add")
async def add_manual(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        text = _str(form, "text")
        try:
            shopping.add_manual(db, shopping.active_list(db), text)
        except shopping.ShoppingError as exc:
            return _notice_redirect(error=str(exc))
    return _notice_redirect(f"Added {text}.")


@router.post("/add-staples")
async def add_staples(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    added = shopping.add_staples(db, shopping.active_list(db))
    return _notice_redirect(f"Added {added} staple{'s' if added != 1 else ''}" if added else None)


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
        try:
            outcome = shopping.add_from_recipe(
                db, shopping.active_list(db), detail.id, servings=servings, missing_only=True
            )
        except shopping.ShoppingError as exc:
            return _notice_redirect(error=str(exc))
    return _notice_redirect(f"{detail.title}: {outcome.notice}")


# --------------------------------------------------------------------------------------
# Trip builder: pick recipes -> preview -> apply
# --------------------------------------------------------------------------------------


@router.get("/plan")
def plan(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return render(
        request, "shopping/plan.html", active_nav="shopping", user=user,
        cookbook=recipes.list_recipes(db, status="cookbook"),
        inbox=recipes.list_recipes(db, status="inbox"),
    )


@router.post("/plan/preview")
async def plan_preview(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        picks = _picks_from_form(form)
    if not picks:
        return RedirectResponse("/shopping/plan", status_code=303)
    preview = shopping.build_trip(db, picks)
    return render(
        request, "shopping/preview.html", active_nav="shopping", user=user, preview=preview
    )


@router.post("/plan/apply")
async def plan_apply(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        picks = _picks_from_form(form)
        included = {v for v in form.getlist("line") if isinstance(v, str)}
    if not picks:
        return RedirectResponse("/shopping/plan", status_code=303)
    # The form posts the lines to KEEP; everything else in the recomputed preview is excluded.
    preview = shopping.build_trip(db, picks)
    exclude = {line.key for line in preview.to_buy if line.key not in included}
    added = shopping.apply_trip(db, shopping.active_list(db), picks, exclude=exclude)
    return _notice_redirect(f"Trip added: {added} item{'s' if added != 1 else ''}")


# --------------------------------------------------------------------------------------
# Done shopping -> restock review
# --------------------------------------------------------------------------------------


@router.get("/restock")
def restock(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    list_id = shopping.active_list(db)
    candidates = shopping.restock_candidates(db, list_id)
    if not candidates:
        return _notice_redirect(
            "Nothing on the list is tracked in the pantry, so there is nothing to put away."
        )
    return render(
        request, "shopping/restock.html", active_nav="shopping", user=user,
        candidates=candidates, locations=pantry.list_locations(db),
    )


@router.post("/restock/apply")
async def restock_apply(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        restock_ids = {
            int(v) for v in form.getlist("restock") if isinstance(v, str) and v.isdigit()
        }
        create_ids = {
            int(v) for v in form.getlist("create") if isinstance(v, str) and v.isdigit()
        }
        seen_ids = {
            int(v) for v in form.getlist("seen") if isinstance(v, str) and v.isdigit()
        }
        location_raw = _str(form, "create_location")
        location_id = int(location_raw) if location_raw.isdigit() else None
        create_locations: dict[int, int] = {}
        for item_id in create_ids:
            per_line = _str(form, f"loc_{item_id}")
            if per_line.isdigit():
                create_locations[item_id] = int(per_line)
    summary = shopping.apply_restock(
        db, shopping.active_list(db), restock_item_ids=restock_ids,
        create_item_ids=create_ids, create_location_id=location_id,
        create_locations=create_locations, clear_item_ids=seen_ids or None, user_id=user.id,
    )
    notice = f"Pantry updated: {', '.join(summary)}" if summary else "List cleared"
    return _notice_redirect(notice)


# --------------------------------------------------------------------------------------
# Line + purchase-info mutations, export
# --------------------------------------------------------------------------------------


@router.post("/foods/{food_id}/purchase")
async def set_purchase(
    request: Request,
    food_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        try:
            shopping.set_purchase_info(
                db, food_id, quantity_text=_str(form, "quantity") or None,
                unit=_str(form, "unit") or None, label=_str(form, "label") or None,
            )
        except shopping.ShoppingError as exc:
            return _notice_redirect(error=str(exc))
    return _notice_redirect("Saved how you buy this.")


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
    if request.headers.get("HX-Request"):
        # Swap the one row (plus an out-of-band count and nav badge) so checking things off
        # in the store never reloads the page or throws away her scroll position.
        list_id = shopping.active_list(db)
        item = shopping.get_item(db, list_id, item_id)
        if item is None:
            return HTMLResponse("")
        remaining, total = shopping.counts(db, list_id)
        response = render(
            request, "shopping/_row_swap.html", user=user, item=item,
            sources=shopping.sources_by_item(db, list_id),
            remaining=remaining, total=total, oob=True,
        )
        response.headers["HX-Trigger"] = "rc:shopping-changed"
        return response
    return _notice_redirect()


@router.post("/items/{item_id}/remove")
async def remove(
    request: Request,
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    shopping.remove(db, item_id)
    return _notice_redirect()


@router.post("/clear-checked")
async def clear_checked(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    shopping.clear_checked(db, shopping.active_list(db))
    return _notice_redirect()


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
