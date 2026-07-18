"""Pantry-aware recipe matching (Phase 4e): "you have 7 of 9" and the "use it up" rail.

Recipes and the pantry join on food_id. Matchable ingredients exclude to_taste and unquantified
lines so a missing salt entry never makes a recipe look impossible (docs/06 section 2). "Use it up"
reranks the cookbook by how many soon-to-expire pantry items a recipe would consume - Grocy's Due
Score, reborn as a suggestion rather than a chore.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from app.security import now
from app.services import quantity, recipes


@dataclass(frozen=True, slots=True)
class RecipeCoverage:
    have: int
    total: int
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.have == self.total


def _pantry_has(conn: sqlite3.Connection, food_id: int) -> bool:
    """True if any pantry item for this food is on hand (exact > 0, gauge not out, or has it)."""
    rows = conn.execute(
        "SELECT quantity_mode, canonical_quantity, quantity_text, gauge, have "
        "FROM pantry_items WHERE food_id = ?",
        (food_id,),
    ).fetchall()
    for r in rows:
        mode = r["quantity_mode"]
        if mode == "exact":
            if (r["canonical_quantity"] or 0) > 0:
                return True
            if r["quantity_text"] and quantity.parse_quantity(r["quantity_text"]) > 0:
                return True
        elif (mode == "gauge" and r["gauge"] != "out") or (mode == "binary" and r["have"]):
            return True
    return False


def _matchable(detail: recipes.RecipeDetail) -> list[recipes.IngredientView]:
    """Ingredients that count toward coverage: a real food + a real amount, not to-taste."""
    return [
        ing
        for ing in detail.ingredients
        if ing.food_id is not None
        and ing.quantity_text is not None
        and ing.scaling_mode != "to_taste"
    ]


def recipe_coverage(conn: sqlite3.Connection, recipe_id: int) -> RecipeCoverage:
    """How many of a recipe's matchable ingredients the pantry currently has."""
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        return RecipeCoverage(have=0, total=0)
    matchable = _matchable(detail)
    have = 0
    missing: list[str] = []
    for ing in matchable:
        assert ing.food_id is not None  # guaranteed by _matchable
        if _pantry_has(conn, ing.food_id):
            have += 1
        else:
            missing.append(ing.food_name or ing.original_text)
    return RecipeCoverage(have=have, total=len(matchable), missing=missing)


@dataclass(frozen=True, slots=True)
class UseItUp:
    slug: str
    title: str
    uses: int  # how many soon-to-expire pantry foods this recipe consumes


def use_it_up(
    conn: sqlite3.Connection, *, within_days: int = 7, status: str = "cookbook", limit: int = 20
) -> list[UseItUp]:
    """Cookbook recipes ranked by how many pantry items expiring within `within_days` they use."""
    cutoff = (now() + timedelta(days=within_days)).date().isoformat()
    priority = {
        int(r["food_id"])
        for r in conn.execute(
            "SELECT DISTINCT food_id FROM pantry_items "
            "WHERE food_id IS NOT NULL AND expires_on IS NOT NULL AND expires_on <= ?",
            (cutoff,),
        ).fetchall()
    }
    if not priority:
        return []
    ranked: list[UseItUp] = []
    for r in conn.execute(
        "SELECT id, slug, title FROM recipes WHERE status = ?", (status,)
    ).fetchall():
        foods = {
            int(row["food_id"])
            for row in conn.execute(
                "SELECT DISTINCT food_id FROM recipe_ingredients "
                "WHERE recipe_id = ? AND food_id IS NOT NULL",
                (r["id"],),
            ).fetchall()
        }
        uses = foods & priority
        if uses:
            ranked.append(UseItUp(slug=r["slug"], title=r["title"], uses=len(uses)))
    ranked.sort(key=lambda u: (-u.uses, u.title.lower()))
    return ranked[:limit]
