"""Primary navigation: Home (discovery), the library tabs, the ingest inbox, and Add a recipe.

Home leads with food - tonight's picks, use-it-up, almost-have-it, new-to-try - instead of a
triage queue (docs/07 section 2). The Cookbook composes FTS search with tier/tag/time/rating
filter chips and links to the "what can I make now?" pantry view. /add is the PC counterpart of
the iPhone Shortcut (docs/04 section 1.3): paste, drop, or bookmarklet a link.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
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


def library_url(
    path: str,
    *,
    query: str = "",
    tags: Sequence[str] = (),
    tier: str = "",
    maxmin: str = "",
    rating: str = "",
    page: int = 1,
) -> str:
    """A library URL carrying exactly the filters that are on.

    Built here rather than in the template: tags are repeated ``?tag=`` parameters now, and
    hand-assembling those in Jinja is how a filter quietly gets dropped from a page link.
    """
    parts: list[tuple[str, str]] = []
    if query:
        parts.append(("q", query))
    parts += [("tag", tag) for tag in tags]
    if tier:
        parts.append(("tier", tier))
    if maxmin:
        parts.append(("maxmin", maxmin))
    if rating:
        parts.append(("rating", rating))
    if page > 1:
        parts.append(("page", str(page)))
    return f"{path}?{urlencode(parts)}" if parts else path


def _toggled(current: Sequence[str], value: str) -> list[str]:
    """The tag set with ``value`` added, or removed if it is already on."""
    lowered = value.lower()
    if any(tag.lower() == lowered for tag in current):
        return [tag for tag in current if tag.lower() != lowered]
    return [*current, value]


@dataclass(frozen=True, slots=True)
class Chip:
    """One filter chip: what it says, where it goes, and whether it is currently on."""

    label: str
    url: str
    is_on: bool


def _filter_chips(
    tag_counts: Sequence[recipe_service.TagCount],
    *,
    tags: Sequence[str],
    tier: str,
    maxmin: str,
    rating: str,
    query: str,
) -> list[Chip]:
    """The cookbook's filter bar. Every chip toggles itself and keeps the others, so filters
    stack - "chicken + weeknight + 8 stars" - instead of replacing each other."""

    def url(**changed: Any) -> str:
        base: dict[str, Any] = {
            "query": query, "tags": tags, "tier": tier, "maxmin": maxmin, "rating": rating,
        }
        base.update(changed)
        return library_url("/cookbook", **base)

    chips = [
        Chip("★ 8+", url(rating="" if rating == "8" else "8"), rating == "8"),
        Chip("≤ 45 min", url(maxmin="" if maxmin == "45" else "45"), maxmin == "45"),
    ]
    for name in ("meal_prep", "family", "company"):
        chips.append(
            Chip(name.replace("_", " "), url(tier="" if tier == name else name), tier == name)
        )
    lowered = {tag.lower() for tag in tags}
    chips += [
        Chip(tc.name, url(tags=_toggled(tags, tc.name)), tc.name.lower() in lowered)
        for tc in tag_counts
    ]
    # A tag that is filtered on but outside the top-N chips still needs a way off.
    known = {tc.name.lower() for tc in tag_counts}
    chips += [
        Chip(tag, url(tags=_toggled(tags, tag)), True)
        for tag in tags
        if tag.lower() not in known
    ]
    return chips


def _render_library(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    status: str,
    title: str,
    query: str | None,
    *,
    tags: Sequence[str] = (),
    tier: str | None = None,
    maxmin: str | None = None,
    rating: str | None = None,
    page: int = 1,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    status_code: int = 200,
    extra: dict[str, Any] | None = None,
) -> Response:
    filters: dict[str, Any] = {
        "status": status, "query": query, "tags": tags, "tier": tier,
        "max_minutes": _int_param(maxmin), "min_rating": _int_param(rating),
    }
    total = recipe_service.count_recipes(db, **filters)
    page_size = recipe_service.PAGE_SIZE
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    recipes_list = recipe_service.list_recipes(
        db, **filters, limit=page_size, offset=(page - 1) * page_size
    )
    coverage = matching.batch_coverage(db, status=status) if status == "cookbook" else {}
    accepted = recipe_service.normalize_tag_filters(tags)
    path = f"/{'archive' if status == 'archived' else status}"
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
        filter_tags=accepted,
        filter_tier=tier or "",
        filter_maxmin=maxmin or "",
        filter_rating=rating or "",
        chips=_filter_chips(
            recipe_service.list_tags(db, status=status) if status == "cookbook" else [],
            tags=accepted, tier=tier or "", maxmin=maxmin or "", rating=rating or "",
            query=query or "",
        ) if status == "cookbook" else [],
        total=total,
        page=page,
        pages=pages,
        showing_from=0 if total == 0 else (page - 1) * page_size + 1,
        showing_to=min(total, page * page_size),
        prev_url=library_url(
            path, query=query or "", tags=accepted, tier=tier or "", maxmin=maxmin or "",
            rating=rating or "", page=page - 1,
        ) if page > 1 else "",
        next_url=library_url(
            path, query=query or "", tags=accepted, tier=tier or "", maxmin=maxmin or "",
            rating=rating or "", page=page + 1,
        ) if page < pages else "",
        notice=notice, error=error, undo=undo, undo_kind=undo_kind,
        status_code=status_code,
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
    page: int = 1,
    status_code: int = 200,
) -> Response:
    # Delegates so the paging context is assembled in exactly one place: browse.html now needs
    # a page/total/prev/next set, and a second hand-built context is where one goes missing.
    return _render_library(
        request, db, user, "inbox", "Inbox", query, page=page,
        notice=notice, undo=undo, undo_kind=undo_kind,
        status_code=status_code,
        extra={"ingest_error": error, **_jobs_context(db)},
    )


@router.get("/inbox")
def inbox(
    request: Request,
    q: str | None = None,
    page: int = 1,
    notice: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _inbox_response(
        request, db, user, query=q, page=page, notice=notice, undo=undo, undo_kind=undo_kind
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


def _unpaged_context(count: int) -> dict[str, Any]:
    """browse.html's paging/filter variables for the two curated views that do not paginate.

    Spelled out rather than left undefined so a template that grows a new reference fails in
    tests, not on the family's phone.
    """
    return {
        "coverage": {}, "chips": [], "filter_tags": [],
        "filter_tier": "", "filter_maxmin": "", "filter_rating": "",
        "total": count, "page": 1, "pages": 1,
        "showing_from": 1 if count else 0, "showing_to": count,
        "prev_url": "", "next_url": "",
    }


@router.get("/cookbook")
def cookbook(
    request: Request,
    q: str | None = None,
    sort: str | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    tier: str | None = None,
    maxmin: str | None = None,
    rating: str | None = None,
    page: int = 1,
    notice: str | None = None,
    error: str | None = None,
    undo: int | None = None,
    undo_kind: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    if sort == "stale":
        stale = cooking.list_recipes_by_staleness(
            db, status="cookbook", limit=recipe_service.PAGE_SIZE
        )
        return render(
            request, "recipes/browse.html", active_nav="cookbook", user=user,
            tab_title="Cookbook", status="cookbook", query="", stale_sort=True,
            recipes=stale, **_unpaged_context(len(stale)),
        )
    if sort == "useitup":
        use_it_up = matching.use_it_up(db)
        return render(
            request, "recipes/browse.html", active_nav="cookbook", user=user,
            tab_title="Cookbook", status="cookbook", query="", use_it_up=use_it_up,
            recipes=[], **_unpaged_context(len(use_it_up)),
        )
    return _render_library(
        request, db, user, "cookbook", "Cookbook", q,
        tags=tag or (), tier=tier, maxmin=maxmin, rating=rating, page=page,
        notice=notice, error=error, undo=undo, undo_kind=undo_kind,
    )


@router.get("/can-make")
def can_make(
    request: Request,
    tag: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    accepted = recipe_service.normalize_tag_filters(tag or ())
    tag_counts = recipe_service.list_tags(db, status="cookbook")
    return render(
        request, "recipes/can_make.html", active_nav="cookbook", user=user,
        groups=discovery.can_make(db, tags=accepted, query=q), query=q or "",
        filter_tags=accepted,
        chips=[
            Chip(
                tc.name,
                library_url(
                    "/can-make", query=q or "", tags=_toggled(accepted, tc.name)
                ),
                any(t.lower() == tc.name.lower() for t in accepted),
            )
            for tc in tag_counts
        ],
    )


@router.get("/archive")
def archive(
    request: Request,
    q: str | None = None,
    page: int = 1,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render_library(request, db, user, "archived", "Archive", q, page=page)


