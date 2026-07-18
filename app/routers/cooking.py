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
from app.services import cooking, deductions, recipes
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
            cook_log_id = cooking.record_cook(db, detail.id, data, user_id=user.id)
        except cooking.CookError as exc:
            return render(
                request, "cook/after_cook.html", active_nav=None, user=user, recipe=detail,
                error=str(exc), status_code=400,
            )
    # Cook recorded. If the pantry has anything to deduct, either auto-apply a trusted recipe or
    # send the user to review the proposal first (docs/06 §2.1).
    proposal = deductions.propose(
        db, detail.id, servings_made=data.servings_made, cook_log_id=cook_log_id
    )
    if not proposal.deductible_lines:
        return RedirectResponse(f"/recipes/{slug}", status_code=303)
    if proposal.auto_ready:
        eligible = {line.ingredient_id for line in proposal.deductible_lines if line.eligible}
        result = deductions.apply(
            db, detail.id, cook_log_id, line_ids=eligible,
            servings_made=data.servings_made, user_id=user.id,
        )
        return RedirectResponse(
            f"/recipes/{slug}/deductions?cook={cook_log_id}&applied={result.batch_id}",
            status_code=303,
        )
    return RedirectResponse(f"/recipes/{slug}/deductions?cook={cook_log_id}", status_code=303)


# --------------------------------------------------------------------------------------
# Cook-through pantry deductions (Phase 4c): review -> apply -> undo
# --------------------------------------------------------------------------------------


@router.get("/{slug}/deductions")
def deductions_review(
    request: Request,
    slug: str,
    cook: int,
    applied: str | None = None,
    servings: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/cookbook", status_code=303)
    if applied:  # already applied (auto-apply or after a review submit): show summary + Undo
        return render(
            request, "cook/deductions.html", active_nav=None, user=user, recipe=detail,
            proposal=None, applied=applied, summary=deductions.batch_summary(db, applied),
            cook_log_id=cook,
        )
    proposal = deductions.propose(db, detail.id, servings_made=servings, cook_log_id=cook)
    return render(
        request, "cook/deductions.html", active_nav=None, user=user, recipe=detail,
        proposal=proposal, applied=None, summary=None, cook_log_id=cook,
    )


@router.post("/{slug}/deductions")
async def deductions_apply(
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
        cook_log_id = _form_int(form, "cook_log_id")
        servings = _form_str(form, "servings") or None
        line_ids = {int(v) for v in form.getlist("line") if isinstance(v, str) and v.isdigit()}
        result = deductions.apply(
            db, detail.id, cook_log_id, line_ids=line_ids, servings_made=servings,
            trust=_form_str(form, "trust") in ("on", "1", "true"),
            auto=_form_str(form, "auto") in ("on", "1", "true"), user_id=user.id,
        )
    return RedirectResponse(
        f"/recipes/{slug}/deductions?cook={cook_log_id}&applied={result.batch_id}", status_code=303
    )


@router.post("/{slug}/deductions/undo")
async def deductions_undo(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        batch_id = _form_str(form, "batch_id")
        if batch_id:
            deductions.undo(db, batch_id, user_id=user.id)
    return RedirectResponse(f"/recipes/{slug}", status_code=303)
