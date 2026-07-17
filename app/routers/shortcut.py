"""'Share from your phone' setup: mint a personal ingest token and show Shortcut instructions.

Any signed-in family member can create their own ingest-scoped token here (not just an admin) and
follow the steps to build an Apple Shortcut that shares a recipe or YouTube link straight into the
inbox. The freshly minted token is shown exactly once, by rendering the page directly.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response

from app.auth import current_user, require_csrf
from app.config import get_settings
from app.deps import get_db
from app.services import tokens
from app.services.users import User
from app.templating import render

router = APIRouter()


def _render(request: Request, db: sqlite3.Connection, user: User, **extra: Any) -> Response:
    settings = get_settings()
    return render(
        request,
        "shortcut.html",
        active_nav=None,
        user=user,
        ingest_url=f"{settings.app_base_url}/api/ingest",
        my_tokens=tokens.list_tokens_for_user(db, user.id),
        **extra,
    )


@router.get("/shortcut")
def shortcut_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render(request, db, user)


@router.post("/shortcut/token")
def create_my_token(
    request: Request,
    label: str = Form("iPhone"),
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    clean_label = label.strip() or "iPhone"
    raw = tokens.create_ingest_token(db, user.id, clean_label)
    return _render(request, db, user, new_token=raw, new_label=clean_label)
