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
from app.services import foods, quantity, recipes


@dataclass(frozen=True, slots=True)
class SubSuggestion:
    """A stand-in for a missing ingredient: learned from real cooks or a same-family pantry item."""

    text: str
    have: bool  # the pantry currently has it


@dataclass(frozen=True, slots=True)
class MissingIngredient:
    name: str
    food_id: int | None
    subs: list[SubSuggestion] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RecipeCoverage:
    have: int
    total: int
    missing: list[MissingIngredient] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.have == self.total

    @property
    def missing_names(self) -> list[str]:
        return [m.name for m in self.missing]


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


def _sub_suggestions(conn: sqlite3.Connection, food_id: int) -> list[SubSuggestion]:
    """What could stand in for a missing food: learned subs first, then same-family pantry items."""
    suggestions: list[SubSuggestion] = []
    seen: set[str] = set()
    for option in foods.substitutes_for(conn, food_id):
        have = bool(
            option.substitute_food_id is not None
            and _pantry_has(conn, option.substitute_food_id)
        )
        key = option.substitute_text.lower()
        if key not in seen:
            seen.add(key)
            suggestions.append(SubSuggestion(text=option.substitute_text, have=have))
    for family_food_id in sorted(foods.family_ids(conn, food_id) - {food_id}):
        if not _pantry_has(conn, family_food_id):
            continue
        row = conn.execute("SELECT name FROM foods WHERE id = ?", (family_food_id,)).fetchone()
        if row is None:
            continue
        key = str(row["name"]).lower()
        if key not in seen:
            seen.add(key)
            suggestions.append(SubSuggestion(text=str(row["name"]), have=True))
    return suggestions[:4]


def recipe_coverage(
    conn: sqlite3.Connection, recipe_id: int, *, with_subs: bool = True
) -> RecipeCoverage:
    """How many of a recipe's matchable ingredients the pantry currently has."""
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        return RecipeCoverage(have=0, total=0)
    matchable = _matchable(detail)
    have = 0
    missing: list[MissingIngredient] = []
    for ing in matchable:
        assert ing.food_id is not None  # guaranteed by _matchable
        if _pantry_has(conn, ing.food_id):
            have += 1
        else:
            missing.append(
                MissingIngredient(
                    name=ing.food_name or ing.original_text,
                    food_id=ing.food_id,
                    subs=_sub_suggestions(conn, ing.food_id) if with_subs else [],
                )
            )
    return RecipeCoverage(have=have, total=len(matchable), missing=missing)


def batch_coverage(
    conn: sqlite3.Connection, *, status: str = "cookbook"
) -> dict[int, RecipeCoverage]:
    """Coverage for every recipe in a status at once (cards, Home, "what can I make").

    One pass over recipe_ingredients + one pantry-presence set; no sub suggestions (those are
    per-recipe detail). Matchability mirrors _matchable: a food, an amount, not to-taste.
    """
    present: set[int] = set()
    for row in conn.execute(
        "SELECT DISTINCT food_id FROM pantry_items WHERE food_id IS NOT NULL"
    ).fetchall():
        if _pantry_has(conn, int(row["food_id"])):
            present.add(int(row["food_id"]))
    out: dict[int, RecipeCoverage] = {}
    rows = conn.execute(
        """SELECT r.id AS recipe_id, ri.food_id, ri.quantity_text, ri.scaling_mode,
                  ri.original_text, fo.name AS food_name
           FROM recipes r JOIN recipe_ingredients ri ON ri.recipe_id = r.id
           LEFT JOIN foods fo ON fo.id = ri.food_id
           WHERE r.status = ? ORDER BY r.id, ri.sort_order""",
        (status,),
    ).fetchall()
    counts: dict[int, tuple[int, int, list[MissingIngredient]]] = {}
    for r in rows:
        if (
            r["food_id"] is None
            or r["quantity_text"] is None
            or r["scaling_mode"] == "to_taste"
        ):
            continue
        have, total, missing = counts.get(int(r["recipe_id"]), (0, 0, []))
        total += 1
        if int(r["food_id"]) in present:
            have += 1
        else:
            missing.append(
                MissingIngredient(
                    name=r["food_name"] or r["original_text"], food_id=int(r["food_id"])
                )
            )
        counts[int(r["recipe_id"])] = (have, total, missing)
    for recipe_id, (have, total, missing) in counts.items():
        out[recipe_id] = RecipeCoverage(have=have, total=total, missing=missing)
    return out


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
