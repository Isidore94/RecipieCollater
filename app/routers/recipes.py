"""Manual recipe screens: create, view, edit, delete, and status changes (Phase 1).

Ingredient rows arrive as parallel form arrays (ing_qty, ing_unit, ing_food, ...) so the
number of rows is dynamic; blank rows are dropped. The recipe/edit form renders from a plain
dict "model" shared by the new (empty), edit (existing), and validation-error (resubmitted)
paths, so a rejected submission keeps what the user typed.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.datastructures import FormData, UploadFile

from app.auth import current_user, require_csrf
from app.config import get_settings
from app.deps import get_db
from app.extraction import ExtractedRecipe
from app.services import ai_draft, cooking, matching, quantity, recipes
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/recipes")

_BLANK_ROWS = 3
_ALLOWED_IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _image_upload(form: FormData) -> UploadFile | None:
    value = form.get("image")
    return value if isinstance(value, UploadFile) else None


async def _save_image(recipe_id: int, upload: UploadFile | None) -> str | None:
    """Save an uploaded recipe photo under data/images/<recipe_id>/ and return its relative path."""
    if upload is None or not upload.filename:
        return None
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        return None
    data = await upload.read()
    if not data or len(data) > _MAX_IMAGE_BYTES:
        return None
    images_dir = get_settings().images_dir
    (images_dir / str(recipe_id)).mkdir(parents=True, exist_ok=True)
    relative = f"{recipe_id}/image{ext}"
    (images_dir / relative).write_bytes(data)
    return relative


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


def _model_from_extracted(extracted: ExtractedRecipe) -> dict[str, Any]:
    """Map an AI-drafted recipe into the manual-entry form model so the cook can review + save."""
    data = recipes.RecipeInput(
        title=extracted.title,
        description=extracted.description,
        servings_text=extracted.servings_text,
        prep_minutes=extracted.prep_minutes,
        cook_minutes=extracted.cook_minutes,
        total_minutes=extracted.total_minutes,
        source_type="manual",
        source_name=extracted.source_name,
        ingredients=[
            recipes.IngredientInput(
                original_text=i.original_text, section=i.section, quantity_text=i.quantity_text,
                unit=i.unit, food=(i.food or i.original_text or None), note=i.note,
            )
            for i in extracted.ingredients
        ],
        steps=[
            recipes.StepInput(instruction=s.instruction, section=s.section, minutes=s.minutes)
            for s in extracted.steps
        ],
        tags=list(extracted.tags),
    )
    return _model_from_input(data)


def _render_form(
    request: Request,
    user: User,
    *,
    action: str,
    model: dict[str, Any],
    heading: str,
    error: str | None = None,
    notice: str | None = None,
    show_draft: bool = False,
    draft_description: str = "",
    cancel_href: str = "/inbox",
    status_code: int = 200,
) -> Response:
    return render(
        request, "recipes/form.html", user=user, action=action, form=model, heading=heading,
        blank_rows=_BLANK_ROWS, error=error, notice=notice, show_draft=show_draft,
        draft_description=draft_description, cancel_href=cancel_href, status_code=status_code,
    )


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


@router.get("/new")
def new_form(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_form(
        request, user, action="/recipes/new", model=_model(), heading="New recipe", show_draft=True
    )


@router.post("/draft")
async def draft(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    description = _text(await request.form(), "describe")
    result = ai_draft.draft_from_description(db, description)
    if result.recipe is None:
        return _render_form(
            request, user, action="/recipes/new", model=_model(), heading="New recipe",
            error=result.error, show_draft=True, draft_description=description, status_code=400,
        )
    return _render_form(
        request, user, action="/recipes/new", model=_model_from_extracted(result.recipe),
        heading="New recipe", notice="Drafted with AI - review the details below, then Save.",
        show_draft=True, draft_description=description,
    )


@router.post("/photo-draft")
async def photo_draft(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Transcribe a photographed recipe (cookbook page/card) into the prefilled form (Phase 6)."""
    async with request.form() as form:
        upload = form.get("photo")
        image = await upload.read() if isinstance(upload, UploadFile) and upload.filename else None
    if not image:
        return _render_form(
            request, user, action="/recipes/new", model=_model(), heading="New recipe",
            error="Choose a photo of the recipe first.", show_draft=True, status_code=400,
        )
    result = ai_draft.draft_from_photo(db, image)
    if result.recipe is None:
        return _render_form(
            request, user, action="/recipes/new", model=_model(), heading="New recipe",
            error=result.error, show_draft=True, status_code=400,
        )
    return _render_form(
        request, user, action="/recipes/new", model=_model_from_extracted(result.recipe),
        heading="New recipe", notice="Read from your photo - review the details below, then Save.",
        show_draft=True,
    )


@router.post("/new")
async def create(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        data = _parse_form(form)
        try:
            recipe_id = recipes.create_recipe(db, data, created_by=user.id)
        except ValueError as exc:
            return _render_form(
                request, user, action="/recipes/new", model=_model_from_input(data),
                heading="New recipe", error=str(exc), status_code=400,
            )
        image_path = await _save_image(recipe_id, _image_upload(form))
        if image_path:
            recipes.set_image(db, recipe_id, image_path)
        detail = recipes.get_recipe(db, recipe_id)
        assert detail is not None
        return RedirectResponse(f"/recipes/{detail.slug}", status_code=303)


def _safe_servings(raw: str | None, base: str) -> str:
    if not raw or not raw.strip():
        return base
    try:
        value = quantity.parse_quantity(raw)
    except ValueError:
        return base
    return raw.strip() if value > 0 else base


def _presets(base: str) -> list[str]:
    values = {2, 4, 6, 8}
    with contextlib.suppress(ValueError):
        values.add(int(quantity.parse_quantity(base)))
    return [str(v) for v in sorted(values)]


@router.get("/{slug}")
def view(
    request: Request,
    slug: str,
    servings: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return RedirectResponse("/inbox", status_code=303)
    target = _safe_servings(servings, detail.base_servings)
    return render(
        request, "recipes/view.html", user=user, recipe=detail,
        scaled=recipes.scale_ingredients(detail, target), servings=target,
        base_servings=detail.base_servings, presets=_presets(detail.base_servings),
        cook_log=cooking.list_cook_log(db, detail.id),
        coverage=matching.recipe_coverage(db, detail.id),
    )


@router.get("/{slug}/ingredients")
def ingredients_fragment(
    request: Request,
    slug: str,
    servings: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return Response(status_code=404)
    target = _safe_servings(servings, detail.base_servings)
    return render(
        request, "recipes/_scale_swap.html", user=user, recipe=detail,
        scaled=recipes.scale_ingredients(detail, target), servings=target,
        base_servings=detail.base_servings, presets=_presets(detail.base_servings), oob=True,
    )


@router.get("/{slug}/export.json")
def export_json(
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return JSONResponse(asdict(detail))


@router.get("/{slug}/export.md")
def export_markdown(
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None:
        return Response(status_code=404)
    return Response(recipes.to_markdown(detail), media_type="text/markdown; charset=utf-8")


@router.get("/{slug}/image")
def recipe_image(
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is None or not detail.image_path:
        return Response(status_code=404)
    path = get_settings().images_dir / detail.image_path
    if not path.is_file():
        return Response(status_code=404)
    return FileResponse(path)


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
        heading=f"Edit: {detail.title}", cancel_href=f"/recipes/{slug}",
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
    async with request.form() as form:
        data = _parse_form(form)
        try:
            recipes.update_recipe(db, detail.id, data, saved_by=user.id)
        except ValueError as exc:
            return _render_form(
                request, user, action=f"/recipes/{slug}/edit", model=_model_from_input(data),
                heading=f"Edit: {detail.title}", error=str(exc),
                cancel_href=f"/recipes/{slug}", status_code=400,
            )
        image_path = await _save_image(detail.id, _image_upload(form))
        if image_path:
            recipes.set_image(db, detail.id, image_path)
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


@router.post("/{slug}/rename")
async def rename(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Title-only rename (imported YouTube titles are clickbait); the slug/URL stays stable."""
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is not None:
        with contextlib.suppress(ValueError):
            recipes.set_title(db, detail.id, _text(await request.form(), "title"))
    return RedirectResponse(f"/recipes/{slug}", status_code=303)


@router.post("/{slug}/rating")
async def rate(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is not None:
        with contextlib.suppress(ValueError):
            recipes.set_rating(db, detail.id, int(_text(await request.form(), "rating") or 0))
    return RedirectResponse(f"/recipes/{slug}", status_code=303)


@router.post("/{slug}/notes")
async def save_notes(
    request: Request,
    slug: str,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    detail = recipes.get_recipe_by_slug(db, slug)
    if detail is not None:
        recipes.set_notes(db, detail.id, _text(await request.form(), "notes"))
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
