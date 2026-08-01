"""Household preferences settings (Phase 5b). Thin: parse form -> services.preferences -> redirect.

Cookie-auth browser routes; every POST is CSRF-guarded. These are the assistant's guardrails:
allergy/exclude are hard constraints, the rest are soft, and the weekday/weekend time budgets +
default servings are scalars.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.routers import flash
from app.services import preferences
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/preferences")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


@router.get("")
def index(
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    prefs = preferences.load(db)
    return render(
        request, "preferences/index.html", active_nav="plan", user=user,
        rows=preferences.list_rows(db), scalars=prefs.scalars, notice=notice, error=error,
    )


@router.post("/add")
async def add(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        value = _str(form, "value")
        if not value:
            return flash.redirect("/preferences", error="Type something to add first.")
        try:
            preferences.add_preference(db, _str(form, "kind"), value)
        except preferences.PreferenceError as exc:
            return flash.redirect("/preferences", error=str(exc))
    return flash.redirect("/preferences", notice=f"Added {value}.")


@router.post("/{pref_id}/remove")
async def remove(
    request: Request,
    pref_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    preferences.remove_preference(db, pref_id)
    return flash.redirect("/preferences", notice="Removed.")


@router.post("/scalars")
async def set_scalars(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    rejected: list[str] = []
    async with request.form() as form:
        for key in ("max_weekday_minutes", "max_weekend_minutes", "default_servings", "tier_mix"):
            try:
                preferences.set_scalar(db, key, _str(form, key))
            except preferences.PreferenceError:
                # Save the fields that are valid and name the ones that are not, rather than
                # dropping the whole submission on the floor.
                rejected.append(key.replace("_", " "))
    if rejected:
        return flash.redirect(
            "/preferences", error="Saved, except: " + ", ".join(rejected) + "."
        )
    return flash.redirect("/preferences", notice="Saved.")
