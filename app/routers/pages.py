"""Primary navigation: Home (discovery), the library tabs, and the ingest inbox.

Home leads with food - tonight's picks, use-it-up, almost-have-it, new-to-try - instead of a
triage queue (docs/07 section 2). The Cookbook composes FTS search with tier/tag/time/rating
filter chips and links to the "what can I make now?" pantry view.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.routers.ingest_api import schedule_processing
from app.services import cooking, discovery, ingest, matching
from app.services import recipes as recipe_service
from app.services.users import User
from app.templating import render

router = APIRouter()

_EMPTY_TABS = {
    "plan": ("Plan", "Weekly meal plans arrive with Phase 5."),
    "chat": ("Assistant", "The AI assistant arrives with Phase 5."),
}

# In-flight ingest job statuses -> the label the inbox shows on each job chip.
_JOB_STATUS_LABEL = {
    "queued": "Queued",
    "fetching": "Fetching page",
    "extracting": "Reading recipe",
    "normalizing": "Saving",
    "failed": "Couldn't add",
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
        **(extra or {}),
    )


def _jobs_context(db: sqlite3.Connection) -> dict[str, Any]:
    jobs = ingest.list_pending_jobs(db)
    return {
        "jobs": jobs,
        "jobs_active": any(j.status in ingest.ACTIVE_STATUSES for j in jobs),
        "job_labels": _JOB_STATUS_LABEL,
    }


def _inbox_response(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    *,
    query: str | None = None,
    error: str | None = None,
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
        status_code=status_code,
        **_jobs_context(db),
    )


@router.get("/inbox")
def inbox(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _inbox_response(request, db, user, query=q)


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


def _render_tab(request: Request, user: User, key: str) -> Response:
    title, blurb = _EMPTY_TABS[key]
    return render(
        request, "tab_empty.html", active_nav=None, user=user, tab_title=title, tab_blurb=blurb
    )


@router.get("/plan")
def plan(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "plan")


@router.get("/chat")
def chat(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_tab(request, user, "chat")
