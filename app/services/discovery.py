"""Home-screen discovery + "what can I make now?" (Phase 4.6).

The app opens on food, not chores: rotation nudges ("you loved this, it's been a while"),
soon-to-expire pantry matches, recipes one ingredient short, and new arrivals to try. The same
batch coverage that powers these modules is the deterministic candidate-set builder the Phase-5
assistant will call - built as GUI first so it is proven by daily use.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from app.security import now, to_iso
from app.services import matching, recipes


@dataclass(frozen=True, slots=True)
class TonightPick:
    slug: str
    title: str
    rating: int | None
    image_path: str | None
    last_cooked: str | None
    cook_count: int


def tonight_picks(
    conn: sqlite3.Connection, *, limit: int = 4, rested_days: int = 14
) -> list[TonightPick]:
    """Cookbook favourites that have rested: highest-rated first among recipes not cooked in
    ``rested_days`` (or never), so the good ones resurface instead of being forgotten."""
    cutoff = to_iso(now() - timedelta(days=rested_days))
    rows = conn.execute(
        """SELECT r.slug, r.title, r.rating, r.image_path,
                  MAX(cl.cooked_at) AS last_cooked, COUNT(cl.id) AS cook_count
           FROM recipes r LEFT JOIN cook_log cl ON cl.recipe_id = r.id
           WHERE r.status = 'cookbook'
           GROUP BY r.id
           HAVING MAX(cl.cooked_at) IS NULL OR MAX(cl.cooked_at) <= ?
           ORDER BY (r.rating IS NULL), r.rating DESC,
                    (MAX(cl.cooked_at) IS NULL) DESC, MAX(cl.cooked_at) ASC
           LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [
        TonightPick(
            slug=r["slug"], title=r["title"], rating=r["rating"], image_path=r["image_path"],
            last_cooked=r["last_cooked"], cook_count=int(r["cook_count"]),
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class CoveredRecipe:
    summary: recipes.RecipeSummary
    coverage: matching.RecipeCoverage


@dataclass(frozen=True, slots=True)
class CanMake:
    """The cookbook sorted by how close the pantry gets you: ready, one short, two short."""

    ready: list[CoveredRecipe] = field(default_factory=list)
    missing_one: list[CoveredRecipe] = field(default_factory=list)
    missing_two: list[CoveredRecipe] = field(default_factory=list)


def can_make(
    conn: sqlite3.Connection,
    *,
    status: str = "cookbook",
    tags: Sequence[str] | None = None,
    query: str | None = None,
) -> CanMake:
    coverage = matching.batch_coverage(conn, status=status)
    result = CanMake()
    for summary in recipes.list_recipes(conn, status=status, tags=tags, query=query):
        cov = coverage.get(summary.id)
        if cov is None or cov.total == 0:
            continue
        entry = CoveredRecipe(summary=summary, coverage=cov)
        shortfall = cov.total - cov.have
        if cov.complete:
            result.ready.append(entry)
        elif shortfall == 1:
            result.missing_one.append(entry)
        elif shortfall == 2:
            result.missing_two.append(entry)
    return result


@dataclass(frozen=True, slots=True)
class HomeData:
    tonight: list[TonightPick]
    use_it_up: list[matching.UseItUp]
    almost: list[CoveredRecipe]
    new_to_try: list[recipes.RecipeSummary]


def home(conn: sqlite3.Connection) -> HomeData:
    groups = can_make(conn)
    return HomeData(
        tonight=tonight_picks(conn),
        use_it_up=matching.use_it_up(conn, limit=3),
        almost=groups.missing_one[:4],
        new_to_try=recipes.list_recipes(conn, status="inbox")[:4],
    )
