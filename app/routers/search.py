"""Global search: the nav-shell search box, its typeahead, and the grouped results page.

Thin on purpose: every group is one bounded call into an existing service (FTS for recipes,
name LIKE for pantry items, foods and tags). The reads are cheap enough to run per keystroke
on the LAN, which is what the typeahead does after a 250 ms debounce.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response

from app.auth import current_user
from app.deps import get_db
from app.services import foods as food_service
from app.services import pantry as pantry_service
from app.services import recipes as recipe_service
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/search")

# A query longer than this is a paste accident, not a search; trimming keeps the FTS match
# expression and the LIKE patterns small.
MAX_QUERY_LENGTH = 100
# Rows per non-recipe group on the full page, and per group in the typeahead.
GROUP_LIMIT = 8
SUGGEST_RECIPES = 5
SUGGEST_FOODS = 3
# Recent recipes / tags shown when the box is submitted empty.
RECENT_RECIPES = 6
SUGGESTED_TAGS = 12


def _clean_query(raw: str | None) -> str:
    return " ".join((raw or "").split())[:MAX_QUERY_LENGTH]


def search_url(query: str, *, tags: Sequence[str] = (), page: int = 1) -> str:
    parts: list[tuple[str, str]] = []
    if query:
        parts.append(("q", query))
    parts += [("tag", tag) for tag in tags]
    if page > 1:
        parts.append(("page", str(page)))
    return "/search?" + urlencode(parts) if parts else "/search"


@router.get("")
def results(
    request: Request,
    q: str | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    page: int = 1,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    query = _clean_query(q)
    tags = recipe_service.normalize_tag_filters(tag)
    if not query:
        return render(
            request,
            "search/results.html",
            active_nav=None,
            user=user,
            query="",
            tags=[],
            recent=recipe_service.list_recipes(db, status="cookbook", limit=RECENT_RECIPES),
            suggested_tags=recipe_service.list_tags(db, limit=SUGGESTED_TAGS),
        )
    total = recipe_service.count_recipes(db, query=query, tags=tags, exclude_status="archived")
    page_size = recipe_service.PAGE_SIZE
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    recipes_found = recipe_service.list_recipes(
        db,
        query=query,
        tags=tags,
        exclude_status="archived",
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return render(
        request,
        "search/results.html",
        active_nav=None,
        user=user,
        query=query,
        tags=tags,
        recipes=recipes_found,
        total=total,
        page=page,
        pages=pages,
        showing_from=0 if total == 0 else (page - 1) * page_size + 1,
        showing_to=min(total, page * page_size),
        prev_url=search_url(query, tags=tags, page=page - 1) if page > 1 else "",
        next_url=search_url(query, tags=tags, page=page + 1) if page < pages else "",
        untag_urls={t: search_url(query, tags=[x for x in tags if x != t]) for t in tags},
        pantry_items=pantry_service.list_items(db, query=query, limit=GROUP_LIMIT),
        foods=food_service.list_foods(db, query=query, limit=GROUP_LIMIT),
        found_tags=recipe_service.list_tags(db, query=query, limit=GROUP_LIMIT),
        cookbook_url="/cookbook?" + urlencode({"q": query}),
        pantry_url="/pantry?" + urlencode({"q": query}),
        foods_url="/foods?" + urlencode({"q": query}),
    )


@router.get("/suggest")
def suggest(
    request: Request,
    q: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    query = _clean_query(q)
    if not query:
        return render(request, "search/_suggest.html", user=user, query="")
    return render(
        request,
        "search/_suggest.html",
        user=user,
        query=query,
        recipes=recipe_service.list_recipes(
            db, query=query, exclude_status="archived", prefix=True, limit=SUGGEST_RECIPES
        ),
        foods=food_service.list_foods(db, query=query, limit=SUGGEST_FOODS),
        full_url=search_url(query),
    )
