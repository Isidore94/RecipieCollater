"""Tag upkeep screen. Thin: parse form -> services.tags -> redirect back.

The cookbook's counterpart to /foods. Cookie-auth browser routes; every POST is CSRF-guarded.
This is also the only place that lists every tag rather than the top handful, so it doubles as
the way to reach a tag the filter chips do not have room for.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.routers import flash
from app.services import recipes as recipe_service
from app.services import tags as tag_service
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/tags")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _int(form: FormData, key: str) -> int | None:
    raw = _str(form, key)
    return int(raw) if raw.isdigit() else None


@router.get("")
def index(
    request: Request,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    all_tags = recipe_service.list_tags(db, limit=None)
    return render(
        request, "tags/index.html", active_nav="cookbook", user=user,
        tags=all_tags, vocabulary=tag_service.VOCABULARY,
        drifted=tag_service.drifted([t.name for t in all_tags]),
        max_tag_length=tag_service.MAX_TAG_LENGTH,
        notice=notice, error=error, undo=undo, undo_kind=undo_kind,
    )


@router.post("/{tag_id}/rename")
async def rename(
    request: Request,
    tag_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        try:
            edit = tag_service.rename(db, tag_id, _str(form, "name"))
        except tag_service.TagError as exc:
            return flash.redirect("/tags", error=str(exc))
        return flash.redirect(
            "/tags",
            notice=f"Renamed “{edit.source_name}” to “{edit.target_name}” "
                   f"on {edit.recipes_affected} recipe{'s' if edit.recipes_affected != 1 else ''}.",
        )


@router.post("/{tag_id}/merge")
async def merge(
    request: Request,
    tag_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        target_id = _int(form, "target_id")
        if target_id is None:
            return flash.redirect("/tags", error="Pick which tag to merge this one into.")
        try:
            edit = tag_service.merge(db, tag_id, target_id, edited_by=user.id)
        except tag_service.TagError as exc:
            return flash.redirect("/tags", error=str(exc))
        return flash.redirect(
            "/tags",
            notice=f"Merged “{edit.source_name}” into “{edit.target_name}”.",
            undo=edit.edit_id, undo_kind="tag",
        )


@router.post("/{tag_id}/delete")
async def delete(
    request: Request,
    tag_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    try:
        edit = tag_service.delete(db, tag_id, edited_by=user.id)
    except tag_service.TagError as exc:
        return flash.redirect("/tags", error=str(exc))
    return flash.redirect(
        "/tags",
        notice=f"Removed “{edit.source_name}” from {edit.recipes_affected} "
               f"recipe{'s' if edit.recipes_affected != 1 else ''}.",
        undo=edit.edit_id, undo_kind="tag",
    )


@router.post("/edits/{edit_id}/undo")
async def undo_edit(
    request: Request,
    edit_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Put back exactly what a merge or a delete took."""
    try:
        name = tag_service.undo(db, edit_id)
    except tag_service.TagError as exc:
        return flash.redirect("/tags", error=str(exc))
    return flash.redirect("/tags", notice=f"Brought “{name}” back.")
