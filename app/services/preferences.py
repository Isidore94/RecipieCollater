"""Household preferences (Phase 5b): the assistant's structured guardrails.

allergy + exclude are HARD constraints - the deterministic candidate filter drops any recipe
whose ingredients hit them, before any model call (docs/06 section 4). dislike / diet / equipment /
cuisine_love are SOFT - ranking hints the assistant may weigh but never enforces. Scalar planning
settings (weekday/weekend time budgets, default servings) live in a small key/value table.

Matching against a recipe is intentionally simple and conservative: a hard term matches when it
appears as a whole word in an ingredient's food name or original text. Better to occasionally keep
a safe recipe out of a suggestion than to ever suggest one that hits an allergy.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

_HARD_KINDS: frozenset[str] = frozenset({"allergy", "exclude"})
_SOFT_KINDS: frozenset[str] = frozenset({"dislike", "diet", "equipment", "cuisine_love"})
_ALL_KINDS = _HARD_KINDS | _SOFT_KINDS

_SCALAR_KEYS: frozenset[str] = frozenset(
    {"max_weekday_minutes", "max_weekend_minutes", "default_servings", "tier_mix"}
)


class PreferenceError(ValueError):
    """Invalid preference input (unknown kind/key)."""


@dataclass(frozen=True, slots=True)
class Preferences:
    allergy: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    dislike: list[str] = field(default_factory=list)
    diet: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    cuisine_love: list[str] = field(default_factory=list)
    scalars: dict[str, str] = field(default_factory=dict)

    @property
    def hard_terms(self) -> list[str]:
        """Every allergy/exclude term, lowercased - what the candidate filter screens on."""
        return [t.lower() for t in (*self.allergy, *self.exclude)]

    def for_prompt(self) -> dict[str, object]:
        """A compact dict for the assistant context (only planning-relevant fields; docs/05 §7)."""
        return {
            "allergies_hard": self.allergy,
            "exclusions_hard": self.exclude,
            "dislikes_soft": self.dislike,
            "diet_soft": self.diet,
            "equipment": self.equipment,
            "cuisines_liked": self.cuisine_love,
            **dict(self.scalars),
        }


def add_preference(
    conn: sqlite3.Connection, kind: str, value: str, *, commit: bool = True
) -> None:
    if kind not in _ALL_KINDS:
        raise PreferenceError(f"unknown preference kind: {kind!r}")
    clean = " ".join(value.split())
    if not clean:
        raise PreferenceError("a preference needs a value")
    conn.execute(
        "INSERT OR IGNORE INTO household_preferences (kind, value) VALUES (?, ?)", (kind, clean)
    )
    if commit:
        conn.commit()


def remove_preference(conn: sqlite3.Connection, pref_id: int, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM household_preferences WHERE id = ?", (pref_id,))
    if commit:
        conn.commit()


def set_scalar(conn: sqlite3.Connection, key: str, value: str, *, commit: bool = True) -> None:
    if key not in _SCALAR_KEYS:
        raise PreferenceError(f"unknown planning setting: {key!r}")
    clean = value.strip()
    if not clean:
        conn.execute("DELETE FROM planning_settings WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO planning_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, clean),
        )
    if commit:
        conn.commit()


@dataclass(frozen=True, slots=True)
class PreferenceRow:
    id: int
    kind: str
    value: str


def list_rows(conn: sqlite3.Connection) -> list[PreferenceRow]:
    rows = conn.execute(
        "SELECT id, kind, value FROM household_preferences ORDER BY kind, value COLLATE NOCASE"
    ).fetchall()
    return [PreferenceRow(id=int(r["id"]), kind=r["kind"], value=r["value"]) for r in rows]


def load(conn: sqlite3.Connection) -> Preferences:
    buckets: dict[str, list[str]] = {k: [] for k in _ALL_KINDS}
    for row in conn.execute(
        "SELECT kind, value FROM household_preferences ORDER BY value COLLATE NOCASE"
    ).fetchall():
        buckets.setdefault(row["kind"], []).append(row["value"])
    scalars = {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM planning_settings").fetchall()
    }
    return Preferences(
        allergy=buckets["allergy"], exclude=buckets["exclude"], dislike=buckets["dislike"],
        diet=buckets["diet"], equipment=buckets["equipment"],
        cuisine_love=buckets["cuisine_love"], scalars=scalars,
    )


def recipe_violates_hard(
    conn: sqlite3.Connection, recipe_id: int, hard_terms: list[str]
) -> str | None:
    """The first hard term a recipe's ingredients hit, or None. Whole-word match on food name +
    original text (so 'nut' doesn't trip on 'nutmeg'... it would; callers pass real allergen
    words like 'peanut', 'tree nut' - and we err toward EXCLUDING, which is the safe direction)."""
    if not hard_terms:
        return None
    rows = conn.execute(
        """SELECT ri.original_text, fo.name AS food_name
           FROM recipe_ingredients ri LEFT JOIN foods fo ON fo.id = ri.food_id
           WHERE ri.recipe_id = ?""",
        (recipe_id,),
    ).fetchall()
    haystacks = [
        f"{(r['food_name'] or '')} {(r['original_text'] or '')}".lower() for r in rows
    ]
    for term in hard_terms:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b")
        if any(pattern.search(h) for h in haystacks):
            return term
    return None
