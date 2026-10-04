"""Re-read a recipe's source and compare the new reading with the recipe (docs/04 section 8).

Thin by design: starting, comparing, applying and dismissing all live in
``app.services.reextract``. A re-read only ever produces a draft - nothing here changes a recipe
except ``apply``, which writes the sections the family ticked through the normal edit service.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.routers import flash
from app.routers.ingest_api import schedule_processing
from app.services import recipes, reextract
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/recipes")


def _sheet(recipe_id: int, db: sqlite3.Connection) -> str:
    detail = recipes.get_recipe(db, recipe_id)
    return f"/recipes/{detail.slug}" if detail else "/inbox"


@router.post("/{recipe_id:int}/reread")
async def reread(
    request: Request,
    recipe_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        refetch = form.get("refetch") is not None
    back = _sheet(recipe_id, db)
    try:
        job, created = reextract.start_reread(db, recipe_id, refetch=refetch, submitted_by=user.id)
    except reextract.ReextractError as exc:
        return flash.redirect(back, error=str(exc))
    if created:
        schedule_processing(job.id)
        return flash.redirect(
            back, notice="Reading the source again - a comparison will appear when it's ready."
        )
    return flash.redirect(back, notice="That source is already being read again.")


@router.get("/{recipe_id:int}/compare/{run_id:int}")
def compare(
    request: Request,
    recipe_id: int,
    run_id: int,
    notice: str | None = None,
    error: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    comparison = reextract.build_comparison(db, recipe_id, run_id)
    if comparison is None:
        return flash.redirect("/inbox", error="That reading no longer exists.")
    if comparison.run.state != "draft":
        return flash.redirect(
            f"/recipes/{comparison.recipe.slug}", notice="That reading has already been reviewed."
        )
    return render(
        request,
        "recipes/compare.html",
        user=user,
        recipe=comparison.recipe,
        run=comparison.run,
        sections=comparison.sections,
        any_changed=any(s.changed for s in comparison.sections),
        notice=notice,
        error=error,
    )


@router.post("/{recipe_id:int}/compare/{run_id:int}/apply")
async def apply(
    request: Request,
    recipe_id: int,
    run_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        take_all = form.get("take_all") is not None
        picked = [v for v in form.getlist("take") if isinstance(v, str)]
    here = f"/recipes/{recipe_id}/compare/{run_id}"
    if take_all:
        comparison = reextract.build_comparison(db, recipe_id, run_id)
        if comparison is None:
            return flash.redirect("/inbox", error="That reading no longer exists.")
        picked = reextract.changed_keys(comparison.sections)
    try:
        taken = reextract.apply_sections(db, recipe_id, run_id, picked, applied_by=user.id)
    except reextract.ReextractError as exc:
        return flash.redirect(here, error=str(exc))
    except ValueError as exc:  # the recipe service rejected the merged result
        return flash.redirect(here, error=str(exc))
    labels = [label.lower() for label in reextract.labels_for(taken)]
    return flash.redirect(
        _sheet(recipe_id, db), notice="Updated from the new reading: " + ", ".join(labels) + "."
    )


@router.post("/{recipe_id:int}/compare/{run_id:int}/dismiss")
async def dismiss(
    recipe_id: int,
    run_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    reextract.dismiss_draft(db, recipe_id, run_id, dismissed_by=user.id)
    return flash.redirect(_sheet(recipe_id, db), notice="Kept the recipe as it is.")
