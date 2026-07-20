"""Receipt/order capture UI (Phase 4.7). Thin: parse form -> services.receipts -> render.

Cookie-auth browser routes; every POST is CSRF-guarded. The capture POST calls the AI provider
synchronously (like the manual-draft flow) - a receipt is one bounded vision/text call, and the
LAN user watches the result land on the review screen.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData, UploadFile

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import pantry, receipts
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/receipts")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


@router.get("/new")
def new_form(
    request: Request,
    error: str | None = None,
    user: User = Depends(current_user),
) -> Response:
    return render(
        request, "receipts/new.html", active_nav="pantry", user=user, error=error
    )


@router.post("")
async def capture(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        upload = form.get("photo")
        image: bytes | None = None
        if isinstance(upload, UploadFile) and upload.filename:
            image = await upload.read() or None
        text = _str(form, "order_text") or None
    try:
        result = receipts.capture(db, text=text, image=image, user_id=user.id)
    except receipts.ReceiptError as exc:
        return RedirectResponse(f"/receipts/new?error={quote(str(exc))}", status_code=303)
    if result.receipt_id is None:
        return RedirectResponse(
            f"/receipts/new?error={quote(result.error or 'Could not read that.')}",
            status_code=303,
        )
    return RedirectResponse(f"/receipts/{result.receipt_id}", status_code=303)


@router.get("/{receipt_id}")
def review(
    request: Request,
    receipt_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    try:
        data = receipts.review(db, receipt_id)
    except receipts.ReceiptError:
        return RedirectResponse("/receipts/new", status_code=303)
    return render(
        request, "receipts/review.html", active_nav="pantry", user=user,
        review=data, locations=pantry.list_locations(db),
    )


@router.post("/{receipt_id}/apply")
async def apply(
    request: Request,
    receipt_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        included = {
            int(v) for v in form.getlist("line") if isinstance(v, str) and v.isdigit()
        }
        names: dict[int, str] = {}
        line_locations: dict[int, int] = {}
        for line_id in included:
            names[line_id] = _str(form, f"food_{line_id}")
            per_line = _str(form, f"loc_{line_id}")
            if per_line.isdigit():
                line_locations[line_id] = int(per_line)
        location_raw = _str(form, "track_location")
        location_id = int(location_raw) if location_raw.isdigit() else None
    try:
        summary = receipts.apply(
            db, receipt_id, included_line_ids=included, food_names=names,
            track_location_id=location_id, line_locations=line_locations, user_id=user.id,
        )
    except receipts.ReceiptError:
        return RedirectResponse("/receipts/new", status_code=303)
    notice = f"Receipt applied: {', '.join(summary)}" if summary else "Receipt applied"
    return RedirectResponse(f"/pantry?notice={quote(notice)}", status_code=303)


@router.post("/{receipt_id}/discard")
async def discard(
    request: Request,
    receipt_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    receipts.discard(db, receipt_id)
    return RedirectResponse("/pantry", status_code=303)
