"""Cook mode + cook log (Phase 3): cook from the phone, then capture how it actually went.

Routers stay thin; all logic lives here (CONVENTIONS). Every amount routes through the tested
exact-quantity service (Decimal, canonical integers) - no float math. cook_log_ingredients rows are
write-once snapshots (original_text copied verbatim) so history survives later recipe edits. Cook
step/timer/checklist progress is device-local (localStorage in cook.js), never a server session.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from app.security import now_iso
from app.services import quantity, recipes

# --------------------------------------------------------------------------------------
# Cook mode: step view, inline timers, ingredient checklist
# --------------------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class TimerSpec:
    label: str
    seconds: int


def _unit_seconds(unit: str) -> int:
    first = unit[:1].lower()
    if first == "h":
        return 3600
    if first == "m":
        return 60
    return 1


def find_timers(text: str) -> list[TimerSpec]:
    """Pull startable durations ('20 minutes', '1 hour', '45 sec') out of step text. Pure."""
    timers: list[TimerSpec] = []
    for match in _DURATION_RE.finditer(text):
        seconds = int(Decimal(match.group(1)) * _unit_seconds(match.group(2)))
        if seconds > 0:
            timers.append(TimerSpec(label=match.group(0).strip(), seconds=seconds))
    return timers


@dataclass(frozen=True, slots=True)
class CookStep:
    number: int
    section: str | None
    instruction: str
    minutes: int | None
    video_seconds: int | None
    seek_available: bool
    timers: list[TimerSpec] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CookView:
    slug: str
    title: str
    video_id: str | None
    servings: str
    steps: tuple[CookStep, ...]
    ingredients: tuple[recipes.ScaledIngredient, ...]


def build_cook_view(detail: recipes.RecipeDetail, servings: str) -> CookView:
    """Assemble what cook mode needs: numbered steps with timers + a scaled ingredient list."""
    steps = tuple(
        CookStep(
            number=index + 1,
            section=step.section,
            instruction=step.instruction,
            minutes=step.minutes,
            video_seconds=step.video_seconds,
            seek_available=bool(detail.video_id and step.video_seconds is not None),
            timers=find_timers(step.instruction),
        )
        for index, step in enumerate(detail.steps)
    )
    scaled = tuple(recipes.scale_ingredients(detail, servings))
    return CookView(
        slug=detail.slug,
        title=detail.title,
        video_id=detail.video_id,
        servings=servings,
        steps=steps,
        ingredients=scaled,
    )


# --------------------------------------------------------------------------------------
# After-cook capture (the inbox -> cookbook promotion gate)
# --------------------------------------------------------------------------------------


class CookError(ValueError):
    """Invalid after-cook input (a bad servings/amount string)."""


@dataclass(frozen=True, slots=True)
class CookCaptureInput:
    rating: int | None = None
    servings_made: str | None = None
    active_minutes: int | None = None
    elapsed_minutes: int | None = None
    notes: str | None = None
    promote: bool = False


def _clean(value: str | None) -> str | None:
    text = value.strip() if value else ""
    return text or None


def record_cook(
    conn: sqlite3.Connection, recipe_id: int, data: CookCaptureInput, *, user_id: int | None = None
) -> int:
    """Write a cook_log entry + immutable per-ingredient snapshot; feed our-times + rating; promote.

    Validates every amount before any write, then commits once, so a bad input never leaves a
    half-written cook log. Returns the new cook_log id.
    """
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        raise CookError("recipe not found")

    servings_made = _clean(data.servings_made)
    if servings_made is not None:
        try:
            if quantity.parse_quantity(servings_made) <= 0:
                raise CookError("servings made must be greater than zero")
        except quantity.QuantityError as exc:
            raise CookError("servings made must be a number") from exc
    rating = None if not data.rating else max(1, min(10, int(data.rating)))
    elapsed = data.elapsed_minutes
    active = data.active_minutes
    notes = _clean(data.notes)

    # Compute every planned amount before the first write, so a bad amount never leaves a
    # half-written cook log (validate-before-write, like recipes._validate).
    factor = recipes.scale_factor(detail.base_servings, servings_made or detail.base_servings)
    snapshots = [(ing, _planned(ing, factor)) for ing in detail.ingredients]
    stamp = now_iso()

    cur = conn.execute(
        """INSERT INTO cook_log
           (recipe_id, user_id, cooked_at, servings_made, actual_minutes,
            actual_active_minutes, actual_elapsed_minutes, rating, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (recipe_id, user_id, stamp, servings_made, elapsed, active, elapsed, rating, notes),
    )
    cook_log_id = int(cur.lastrowid) if cur.lastrowid is not None else 0

    for ing, planned_text in snapshots:
        conn.execute(
            """INSERT INTO cook_log_ingredients
               (cook_log_id, ingredient_id, original_text, food_id, planned_quantity_text)
               VALUES (?, ?, ?, ?, ?)""",
            (cook_log_id, ing.id, ing.original_text, ing.food_id, planned_text),
        )

    # Feed the "in our kitchen" time suggestion and mirror the rating onto the recipe.
    conn.execute(
        """UPDATE recipes
           SET our_minutes = COALESCE(?, our_minutes),
               our_active_minutes = COALESCE(?, our_active_minutes),
               rating = COALESCE(?, rating), updated_at = ?
           WHERE id = ?""",
        (elapsed, active, rating, stamp, recipe_id),
    )
    if data.promote and detail.status == "inbox":
        conn.execute(
            "UPDATE recipes SET status = 'cookbook', promoted_at = ?, updated_at = ? WHERE id = ?",
            (stamp, stamp, recipe_id),
        )
    conn.commit()
    return cook_log_id


def _planned(ing: recipes.IngredientView, factor: Decimal) -> str | None:
    """The ingredient's planned amount at the cooked servings, scaled exactly as cook mode showed
    it (fixed/to_taste and unmeasured lines are left verbatim). round_to_package must scale here
    too, or the snapshot would record a different amount than the cook was actually shown."""
    if (
        ing.quantity_text is None
        or ing.unit_name is None
        or ing.scaling_mode in ("fixed", "to_taste")
    ):
        return ing.quantity_text
    package = (
        quantity.parse_quantity(ing.package_quantity_text) if ing.package_quantity_text else None
    )
    scaled = quantity.scale(
        quantity.parse_quantity(ing.quantity_text),
        factor=factor,
        mode=ing.scaling_mode,
        package=package,
    )
    return quantity.format_quantity(scaled) if scaled is not None else ing.quantity_text


# --------------------------------------------------------------------------------------
# Cook-log timeline + "haven't made in a while"
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CookLogEntry:
    id: int
    cooked_at: str
    cook_name: str | None
    servings_made: str | None
    rating: int | None
    active_minutes: int | None
    elapsed_minutes: int | None
    notes: str | None


def list_cook_log(conn: sqlite3.Connection, recipe_id: int) -> list[CookLogEntry]:
    rows = conn.execute(
        """SELECT cl.id, cl.cooked_at, u.name AS cook_name, cl.servings_made, cl.rating,
                  cl.actual_active_minutes, cl.actual_elapsed_minutes, cl.notes
           FROM cook_log cl LEFT JOIN users u ON u.id = cl.user_id
           WHERE cl.recipe_id = ? ORDER BY cl.cooked_at DESC, cl.id DESC""",
        (recipe_id,),
    ).fetchall()
    return [
        CookLogEntry(
            id=int(r["id"]), cooked_at=r["cooked_at"], cook_name=r["cook_name"],
            servings_made=r["servings_made"], rating=r["rating"],
            active_minutes=r["actual_active_minutes"], elapsed_minutes=r["actual_elapsed_minutes"],
            notes=r["notes"],
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class RecipeStaleness:
    slug: str
    title: str
    last_cooked: str | None
    cook_count: int


def list_recipes_by_staleness(
    conn: sqlite3.Connection, *, status: str = "cookbook"
) -> list[RecipeStaleness]:
    """Recipes ordered by "haven't made in a while": never-cooked first, then oldest cook first."""
    rows = conn.execute(
        """SELECT r.slug, r.title, MAX(cl.cooked_at) AS last_cooked, COUNT(cl.id) AS cook_count
           FROM recipes r LEFT JOIN cook_log cl ON cl.recipe_id = r.id
           WHERE r.status = ?
           GROUP BY r.id
           ORDER BY (MAX(cl.cooked_at) IS NULL) DESC, MAX(cl.cooked_at) ASC, r.created_at ASC""",
        (status,),
    ).fetchall()
    return [
        RecipeStaleness(
            slug=r["slug"], title=r["title"], last_cooked=r["last_cooked"],
            cook_count=int(r["cook_count"]),
        )
        for r in rows
    ]
