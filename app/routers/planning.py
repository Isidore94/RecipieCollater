"""Meal-plan week board UI (Phase 5a). Thin: parse form -> services.planning -> redirect/render.

Cookie-auth browser routes; every POST is CSRF-guarded. The board is plain forms so it works with
JavaScript off (tap-to-assign, not drag-only, per docs/07 accessibility). Plan->shopping reuses the
Phase 4.6 trip builder; iCal export is a GET so the phone's calendar can fetch it.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.config import get_settings
from app.deps import get_db
from app.services import planning, recipes, shopping
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/plan")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _week_start(raw: str | None) -> date:
    if raw:
        try:
            return planning.week_start(date.fromisoformat(raw.strip()))
        except ValueError:
            pass
    return planning.week_start()


def _redirect(start: date, notice: str | None = None) -> Response:
    url = f"/plan?start={start.isoformat()}"
    if notice:
        url += f"&notice={quote(notice)}"
    return RedirectResponse(url, status_code=303)


@router.get("")
def board(
    request: Request,
    start: str | None = None,
    notice: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    week = _week_start(start)
    remaining, _total = shopping.counts(db, shopping.active_list(db))
    return render(
        request, "plan/board.html", active_nav="plan", user=user,
        week_start=week.isoformat(),
        prev_week=(week - timedelta(days=7)).isoformat(),
        next_week=(week + timedelta(days=7)).isoformat(),
        this_week=planning.week_start().isoformat(),
        columns=planning.week_board(db, week),
        cookbook=recipes.list_recipes(db, status="cookbook"),
        menus=planning.list_menus(db),
        shopping_remaining=remaining,
        notice=notice,
    )


@router.post("/entry")
async def add_entry(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        plan_date = _str(form, "plan_date")
        slot = _str(form, "slot") or "dinner"
        note = _str(form, "note")
        recipe_raw = _str(form, "recipe_id")
        try:
            if recipe_raw.isdigit():
                planning.add_recipe_entry(
                    db, plan_date, int(recipe_raw), slot=slot,
                    servings_text=_str(form, "servings") or None, user_id=user.id,
                )
            elif note:
                planning.add_note_entry(db, plan_date, note, slot=slot, user_id=user.id)
        except planning.PlanningError:
            pass
    return _redirect(week)


@router.post("/entry/{entry_id}/remove")
async def remove_entry(
    request: Request,
    entry_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        planning.remove_entry(db, entry_id)
    return _redirect(week)


@router.post("/entry/{entry_id}/move")
async def move_entry(
    request: Request,
    entry_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        with contextlib.suppress(planning.PlanningError):
            planning.move_entry(db, entry_id, _str(form, "plan_date"))
    return _redirect(week)


@router.post("/shopping")
async def to_shopping(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        added = planning.plan_to_shopping(db, week)
    return _redirect(week, f"Added {added} item{'s' if added != 1 else ''} to the shopping list")


@router.post("/save-menu")
async def save_menu(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        with contextlib.suppress(planning.PlanningError):
            planning.save_week_as_menu(db, week, _str(form, "name"))
    return _redirect(week, "Saved this week as a menu")


@router.post("/apply-menu")
async def apply_menu(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        menu_raw = _str(form, "menu_id")
        if menu_raw.isdigit():
            with contextlib.suppress(planning.PlanningError):
                planning.apply_menu_to_week(db, int(menu_raw), week, user_id=user.id)
    return _redirect(week, "Menu applied to this week")


@router.post("/menu/{menu_id}/delete")
async def delete_menu(
    request: Request,
    menu_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        week = _week_start(_str(form, "week_start"))
        planning.delete_menu(db, menu_id)
    return _redirect(week)


@router.get("/export.ics")
def export_ical(
    start: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    week = _week_start(start)
    body = planning.week_ical(db, week, app_base_url=get_settings().app_base_url)
    return PlainTextResponse(
        body, media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="mealplan-{week.isoformat()}.ics"'},
    )
