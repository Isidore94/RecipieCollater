"""Manual recipe screens: create, view, edit, delete, and status changes (Phase 1).

Ingredient rows arrive as parallel form arrays (ing_qty, ing_unit, ing_food, ...) so the
number of rows is dynamic; blank rows are dropped. The recipe/edit form renders from a plain
dict "model" shared by the new (empty), edit (existing), and validation-error (resubmitted)
paths, so a rejected submission keeps what the user typed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import recipes
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/recipes")

_BLANK_ROWS = 6


# --------------------------------------------------------------------------------------
# Form parsing
# --------------------------------------------------------------------------------------


def _text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _texts(form: FormData, key: str) -> list[str]:
    return [value if isinstance(value, str) else "" for value in form.getlist(key)]


def _int(form: FormData, key: str) -> int | None:
    raw = _text(form, key)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _parse_ingredients(form: FormData) -> list[recipes.IngredientInput]:
    sections, qtys = _texts(form, "ing_section"), _texts(form, "ing_qty")
    unit_texts, foods = _texts(form, "ing_unit"), _texts(form, "ing_food")
    notes, scalings = _texts(form, "ing_note"), _texts(form, "ing_scaling")
    count = max(len(sections), len(qtys), len(unit_texts), len(foods), len(notes), len(scalings))

    def at(values: list[str], i: int) -> str:
        return values[i].strip() if i < len(values) else ""

    result: list[recipes.IngredientInput] = []
    for i in range(count):
        food, qty, note = at(foods, i), at(qtys, i), at(notes, i)
        if not (food or qty or note):
            continue  # skip empty rows
        result.append(
            recipes.IngredientInput(
                section=at(sections, i) or None,
                quantity_text=qty or None,
                unit=at(unit_texts, i) or None,
                food=food or None,
                note=note or None,
                scaling_mode=at(scalings, i) or "linear",
            )
        )
    return result


def _parse_form(form: FormData) -> recipes.RecipeInput:
    step_lines = [line.strip() for line in _text(form, "steps").splitlines() if line.strip()]
    tags = [tag.strip() for tag in _text(form, "tags").split(",") if tag.strip()]
    return recipes.RecipeInput(
        title=_text(form, "title"),
        tldr=_text(form, "tldr") or None,
        description=_text(form, "description") or None,
        tier=_text(form, "tier") or None,
        base_servings=_text(form, "base_servings") or "4",
        servings_text=_text(form, "servings_text") or None,
        prep_minutes=_int(form, "prep_minutes"),
        cook_minutes=_int(form, "cook_minutes"),
        total_minutes=_int(form, "total_minutes"),
        active_minutes=_int(form, "active_minutes"),
        elapsed_minutes=_int(form, "elapsed_minutes"),
        source_url=_text(form, "source_url") or None,
        source_name=_text(form, "source_name") or None,
        ingredients=_parse_ingredients(form),
        steps=[recipes.StepInput(instruction=line) for line in step_lines],
        tags=tags,
    )


# --------------------------------------------------------------------------------------
# Form model (shared by new / edit / error render)
# --------------------------------------------------------------------------------------


def _blank(value: object) -> object:
    return "" if value is None else value


def _ing_rows_from_views(views: tuple[recipes.IngredientView, ...]) -> list[dict[str, str]]:
    return [
        {
            "section": v.section or "", "qty": v.quantity_text or "", "unit": v.unit_name or "",
            "food": v.food_name or "", "note": v.note or "", "scaling": v.scaling_mode,
        }
        for v in views
    ]


def _ing_rows_from_inputs(inputs: list[recipes.IngredientInput]) -> list[dict[str, str]]:
    return [
        {
            "section": i.section or "", "qty": i.quantity_text or "", "unit": i.unit or "",
            "food": i.food or "", "note": i.note or "", "scaling": i.scaling_mode,
        }
        for i in inputs
    ]


def _model(
    *,
    title: str = "",
    tldr: str = "",
    description: str = "",
    tier: str = "",
    base_servings: str = "4",
    servings_text: str = "",
    times: dict[str, object] | None = None,
    source_url: str = "",
    source_name: str = "",
    ingredients: list[dict[str, str]] | None = None,
    steps_text: str = "",
    tags_text: str = "",
) -> dict[str, Any]:
    model: dict[str, Any] = {
        "title": title, "tldr": tldr, "description": description, "tier": tier,
        "base_servings": base_servings, "servings_text": servings_text,
        "source_url": source_url, "source_name": source_name,
        "ingredients": ingredients or [], "steps_text": steps_text, "tags_text": tags_text,
    }
    model.update(times or {"prep": "", "cook": "", "total": "", "active": "", "elapsed": ""})
    return model


def _model_from_detail(detail: recipes.RecipeDetail) -> dict[str, Any]:
    return _model(
        title=detail.title, tldr=detail.tldr or "", description=detail.description or "",
        tier=detail.tier or "", base_servings=detail.base_servings,
        servings_text=detail.servings_text or "", source_url=detail.source_url or "",
        source_name=detail.source_name or "",
        times={
            "prep": _blank(detail.prep_minutes), "cook": _blank(detail.cook_minutes),
            "total": _blank(detail.total_minutes), "active": _blank(detail.active_minutes),
            "elapsed": _blank(detail.elapsed_minutes),
        },
        ingredients=_ing_rows_from_views(detail.ingredients),
        steps_text="\n".join(s.instruction for s in detail.steps),
        tags_text=", ".join(detail.tags),
    )


def _model_from_input(data: recipes.RecipeInput) -> dict[str, Any]:
    return _model(
        title=data.title, tldr=data.tldr or "", description=data.description or "",
        tier=data.tier or "", base_servings=data.base_servings,
        servings_text=data.servings_text or "", source_url=data.source_url or "",
        source_name=data.source_name or "",
        times={
            "prep": _blank(data.prep_minutes), "cook": _blank(data.cook_minutes),
            "total": _blank(data.total_minutes), "active": _blank(data.active_minutes),
            "elapsed": _blank(data.elapsed_minutes),
        },
        ingredients=_ing_rows_from_inputs(data.ingredients),
        steps_text="\n".join(s.instruction for s in data.steps),
        tags_text=", ".join(data.tags),
    )


def _render_form(
    request: Request,
    user: User,
    *,
    action: str,
    model: dict[str, Any],
    heading: str,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request, "recipes/form.html", user=user, action=action, form=model, heading=heading,
        blank_rows=_BLANK_ROWS, error=error, status_code=status_code,
    )


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


@router.get("/new")
def new_form(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_form(request, user, action="/recipes/new", model=_model(), heading="New recipe")


@router.post("/new")
async def create(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    data = _parse_form(await request.form())
    try:
        recipe_id = recipes.create_recipe(db, data, created_by=user.id)
    except ValueError as exc:
        return _render_form(
            request, user, action="/recipes/new", model=_model_from_input(data),
            heading="New recipe", error=str(exc), status_code=400,
        )
    detail = recipes.get_recipe(db, recipe_id)
    assert detail is not None
    return RedirectResponse(f"/recipes/{detail.slug}", status_code=303)


@router.get("/{slug}")
def view(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/inbox", status_code=303)
    return render(request, "recipes/view.html", user=user, recipe=detail)


@router.get("/{slug}/edit")
def edit_form(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/inbox", status_code=303)
    return _render_form(
        request, user, action=f"/recipes/{slug}/edit", model=_model_from_detail(detail),
        heading=f"Edit: {detail.title}",
    )


@router.post("/{slug}/edit")
async def update(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/inbox", status_code=303)
    data = _parse_form(await request.form())
    try:
        recipes.update_recipe(db, detail.id, data, saved_by=user.id)
    except ValueError as exc:
        return _render_form(
            request, user, action=f"/recipes/{slug}/edit", model=_model_from_input(data),
            heading=f"Edit: {detail.title}", error=str(exc), status_code=400,
        )
    return RedirectResponse(f"/recipes/{slug}", status_code=303)


@router.post("/{slug}/status")
async def change_status(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is not None:
        status = _text(await request.form(), "status")
        if status in recipes.VALID_STATUS:
            recipes.set_status(db, detail.id, status)
    return RedirectResponse(f"/recipes/{slug}", status_code=303)


@router.post("/{slug}/delete")
async def delete(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is not None:
        recipes.delete_recipe(db, detail.id)
    return RedirectResponse("/inbox", status_code=303)
