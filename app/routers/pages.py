"""Primary navigation: Home (discovery), the library tabs, the ingest inbox, and Add a recipe.

Home leads with food - tonight's picks, use-it-up, almost-have-it, new-to-try - instead of a
triage queue (docs/07 section 2). The Cookbook composes FTS search with tier/tag/time/rating
filter chips and links to the "what can I make now?" pantry view. /add is the PC counterpart of
the iPhone Shortcut (docs/04 section 1.3): paste, drop, or bookmarklet a link.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.config import get_settings
from app.deps import get_db
from app.routers import flash
from app.routers.ingest_api import schedule_processing
from app.services import cooking, discovery, ingest, matching
from app.services import recipes as recipe_service
from app.services.users import User
from app.templating import render, safe_url

router = APIRouter()

# In-flight ingest job statuses -> the label the inbox shows on each job chip.
_JOB_STATUS_LABEL = {
    "queued": "Queued",
    "fetching": "Fetching page",
    "extracting": "Reading recipe",
    "normalizing": "Saving",
    "failed": "Couldn't add",
    "done": "Added",
}


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


@router.get("/")
def home(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return render(
        request, "home.html", active_nav="home", user=user, home=discovery.home(db)
    )


def _int_param(raw: str | None) -> int | None:
    if raw and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _render_library(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    status: str,
    title: str,
    query: str | None,
    *,
    tag: str | None = None,
    tier: str | None = None,
    maxmin: str | None = None,
    rating: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Response:
    recipes_list = recipe_service.list_recipes(
        db, status=status, query=query, tag=tag, tier=tier,
        max_minutes=_int_param(maxmin), min_rating=_int_param(rating),
    )
    coverage = matching.batch_coverage(db, status=status) if status == "cookbook" else {}
    return render(
        request,
        "recipes/browse.html",
        active_nav=status if status in ("inbox", "cookbook") else None,
        user=user,
        tab_title=title,
        status=status,
        query=query or "",
        recipes=recipes_list,
        coverage=coverage,
        filter_tag=tag or "",
        filter_tier=tier or "",
        filter_maxmin=maxmin or "",
        filter_rating=rating or "",
        tags=recipe_service.list_tags(db, status=status) if status == "cookbook" else [],
        notice=notice, error=error, undo=undo, undo_kind=undo_kind,
        **(extra or {}),
    )


def _jobs_context(db: sqlite3.Connection) -> dict[str, Any]:
    jobs = ingest.list_pending_jobs(db)
    # A finished job names the recipe it produced, so the inbox can say what it added and
    # link to it rather than leaving her to spot a new card.
    titles: dict[int, tuple[str, str]] = {}
    for job in jobs:
        if job.status == "done" and job.recipe_id is not None:
            recipe = recipe_service.get_recipe(db, job.recipe_id)
            if recipe is not None:
                titles[job.id] = (recipe.title, recipe.slug)
    return {
        "jobs": jobs,
        "jobs_active": any(j.status in ingest.ACTIVE_STATUSES for j in jobs),
        "job_labels": _JOB_STATUS_LABEL,
        "job_recipes": titles,
    }


def _inbox_response(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    *,
    query: str | None = None,
    error: str | None = None,
    notice: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "recipes/browse.html",
        active_nav="inbox",
        user=user,
        tab_title="Inbox",
        status="inbox",
        query=query or "",
        recipes=recipe_service.list_recipes(db, status="inbox", query=query),
        coverage={},
        tags=[],
        filter_tag="", filter_tier="", filter_maxmin="", filter_rating="",
        ingest_error=error,
        notice=notice, undo=undo, undo_kind=undo_kind,
        status_code=status_code,
        **_jobs_context(db),
    )


@router.get("/inbox")
def inbox(
    request: Request,
    q: str | None = None,
    notice: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _inbox_response(
        request, db, user, query=q, notice=notice, undo=undo, undo_kind=undo_kind
    )


@router.post("/inbox/ingest")
async def ingest_from_browser(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        url = _form_str(form, "url")
        html = _form_str(form, "html")
    if not url:
        return _inbox_response(
            request, db, user, error="Enter a recipe link to add.", status_code=400
        )
    try:
        job, created = ingest.enqueue_job(
            db, url, html=html or None, submitted_by=user.id, source="paste"
        )
    except ingest.IngestError as exc:
        return _inbox_response(request, db, user, error=str(exc), status_code=400)
    if created:
        schedule_processing(job.id)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/jobs/{job_id}/retry")
async def retry_job(
    request: Request,
    job_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    """Requeue a failed ingest. The idempotency key blocks re-pasting, so retry has to live here."""
    if ingest.requeue_failed(db, job_id):
        schedule_processing(job_id)
    if request.headers.get("HX-Request"):
        return render(request, "recipes/_ingest_jobs.html", user=user, **_jobs_context(db))
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/jobs/{job_id}/dismiss")
async def dismiss_job(
    request: Request,
    job_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    ingest.discard_failed(db, job_id)
    if request.headers.get("HX-Request"):
        return render(request, "recipes/_ingest_jobs.html", user=user, **_jobs_context(db))
    return RedirectResponse("/inbox", status_code=303)


# --------------------------------------------------------------------------------------
# Add a recipe from a link - the PC counterpart of the iPhone Shortcut (docs/04 section 1.3)
# --------------------------------------------------------------------------------------


def bookmarklet(base_url: str) -> str:
    """A 'send this page' bookmark built from APP_BASE_URL alone (CONVENTIONS 11).

    Dragged onto the bookmarks bar once, it turns any recipe page or YouTube video into a click:
    the same job the Shortcut queues from an iPhone's share sheet, without an install.
    """
    return (
        "javascript:void(window.open('"
        + base_url.rstrip("/")
        + "/add?url='+encodeURIComponent(location.href),'_blank'))"
    )


def _add_response(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    *,
    url: str = "",
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "add.html",
        active_nav="add",
        user=user,
        prefill_url=url,
        bookmarklet=bookmarklet(get_settings().app_base_url),
        notice=notice,
        ingest_error=error,
        status_code=status_code,
        **_jobs_context(db),
    )


@router.get("/add")
def add_page(
    request: Request,
    url: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    """Paste, drop, or bookmarklet a YouTube/recipe-site link from a PC.

    The bookmarklet lands here with ?url= filled in and then waits for a click rather than
    queueing on arrival: a GET must never mutate (CONVENTIONS 7), and the pause is also the
    chance to correct a link the page handed over. `safe_url` keeps a hand-crafted
    'javascript:' value out of the field.
    """
    return _add_response(request, db, user, url=safe_url(url), notice=notice, error=error)


def _already_here(db: sqlite3.Connection, job: ingest.IngestJob) -> str:
    """What to say when a link is already in the system, so a resubmit isn't a silent no-op.

    The URL is its own idempotency key, so re-pasting produces no job and - once the inbox's
    'recently done' window has passed - nothing in the job list either. Without this the page
    just reloaded unchanged.
    """
    if job.recipe_id is not None:
        recipe = recipe_service.get_recipe(db, job.recipe_id)
        if recipe is not None:
            return f"Already added: {recipe.title}. Find it in your inbox or cookbook."
    if job.status == "failed":
        return "That link is already here and it failed - use Try again below."
    return "That link is already being read."


@router.post("/add")
async def add_from_browser(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        url = _form_str(form, "url")
        html = _form_str(form, "html")
    if not url:
        return _add_response(
            request, db, user, error="Enter a recipe link to add.", status_code=400
        )
    try:
        job, created = ingest.enqueue_job(
            db, url, html=html or None, submitted_by=user.id, source="paste"
        )
    except ingest.IngestError as exc:
        return _add_response(request, db, user, url=url, error=str(exc), status_code=400)
    if not created:
        return flash.redirect("/add", notice=_already_here(db, job))
    schedule_processing(job.id)
    # Back to /add rather than /inbox: adding links comes in runs of three or four, and the
    # progress list below the box already says where each one got to.
    return flash.redirect("/add", notice="Reading that link now - it lands in your inbox.")


@router.get("/inbox/jobs")
def inbox_jobs(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    """htmx fragment: the in-flight/failed ingest jobs, self-polling while any is active."""
    return render(request, "recipes/_ingest_jobs.html", user=user, **_jobs_context(db))


@router.get("/cookbook")
def cookbook(
    request: Request,
    q: str | None = None,
    sort: str | None = None,
    tag: str | None = None,
    tier: str | None = None,
    maxmin: str | None = None,
    rating: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    if sort == "stale":
        return render(
            request, "recipes/browse.html", active_nav="cookbook", user=user,
            tab_title="Cookbook", status="cookbook", query="", stale_sort=True,
            recipes=cooking.list_recipes_by_staleness(db, status="cookbook"),
            coverage={}, tags=[],
            filter_tag="", filter_tier="", filter_maxmin="", filter_rating="",
        )
    if sort == "useitup":
        return render(
            request, "recipes/browse.html", active_nav="cookbook", user=user,
            tab_title="Cookbook", status="cookbook", query="", use_it_up=matching.use_it_up(db),
            coverage={}, tags=[],
            filter_tag="", filter_tier="", filter_maxmin="", filter_rating="",
        )
    return _render_library(
        request, db, user, "cookbook", "Cookbook", q,
        tag=tag, tier=tier, maxmin=maxmin, rating=rating,
        notice=notice, error=error, undo=undo, undo_kind=undo_kind,
    )


@router.get("/can-make")
def can_make(
    request: Request,
    tag: str | None = None,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return render(
        request, "recipes/can_make.html", active_nav="cookbook", user=user,
        groups=discovery.can_make(db, tag=tag, query=q), query=q or "", filter_tag=tag or "",
        tags=recipe_service.list_tags(db, status="cookbook"),
    )


@router.get("/archive")
def archive(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render_library(request, db, user, "archived", "Archive", q)


