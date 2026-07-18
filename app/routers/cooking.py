"""Cook mode + after-cook capture (Phase 3). Thin: parse form -> cooking.py -> render/redirect.

Cookie-auth browser routes only (never the ingest token). Every POST is CSRF-guarded. Cook step /
timer / checklist progress is device-local (cook.js + localStorage), never a server session.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import cooking, recipes
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/recipes")


def _form_int(form: FormData, key: str) -> int | None:
    raw = form.get(key)
    if isinstance(raw, str) and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _form_str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


@router.get("/{slug}/cook")
def cook(
    request: Request,
    slug: str,
    servings: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/cookbook", status_code=303)
    view = cooking.build_cook_view(detail, servings or detail.base_servings)
    return render(request, "cook/mode.html", active_nav=None, user=user, recipe=detail, view=view)


@router.get("/{slug}/after-cook")
def after_cook_form(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/cookbook", status_code=303)
    return render(
        request, "cook/after_cook.html", active_nav=None, user=user, recipe=detail, error=None
    )


@router.post("/{slug}/after-cook")
async def record_after_cook(
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
        data = cooking.CookCaptureInput(
            rating=_form_int(form, "rating"),
            servings_made=_form_str(form, "servings_made") or None,
            active_minutes=_form_int(form, "active_minutes"),
            elapsed_minutes=_form_int(form, "elapsed_minutes"),
            notes=_form_str(form, "notes") or None,
            promote=_form_str(form, "promote") in ("on", "true", "1"),
        )
        try:
            cooking.record_cook(db, detail.id, data, user_id=user.id)
        except cooking.CookError as exc:
            return render(
                request, "cook/after_cook.html", active_nav=None, user=user, recipe=detail,
                error=str(exc), status_code=400,
            )
    return RedirectResponse(f"/recipes/{slug}", status_code=303)
