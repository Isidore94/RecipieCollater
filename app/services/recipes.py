"""Recipe CRUD: create/read/update/delete a manual recipe with its ingredients, steps,
and tags, plus status changes and FTS-backed listing (Phase 1 manual cookbook).

All amount strings are validated with app.services.quantity (CONVENTIONS 1); units and foods
resolve through the ontology (foods are created on demand for manual entry). Input is fully
validated before any row is written, so a bad ingredient never leaves a half-written recipe.
Every update snapshots the prior recipe as JSON into recipe_revisions for cheap undo. FTS stays
in sync via the migration-006 triggers, so writes here never touch recipe_fts directly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field

from app.security import now_iso
from app.services import quantity, units

VALID_STATUS: frozenset[str] = frozenset({"inbox", "cookbook", "archived"})
VALID_TIER: frozenset[str] = frozenset({"meal_prep", "family", "company"})
VALID_SOURCE_TYPE: frozenset[str] = frozenset({"youtube", "web", "manual", "photo"})


class RecipeError(ValueError):
    """Invalid recipe input (bad amount, unit, scaling mode, tier, status, or source)."""


# --------------------------------------------------------------------------------------
# Input models (what a form/importer supplies)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngredientInput:
    original_text: str = ""
    section: str | None = None
    quantity_text: str | None = None
    unit: str | None = None  # free text, resolved to a unit_id
    food: str | None = None  # free text, resolved or created to a food_id
    note: str | None = None
    scaling_mode: str = "linear"
    package_quantity_text: str | None = None
    package_unit: str | None = None


@dataclass(frozen=True, slots=True)
class StepInput:
    instruction: str
    section: str | None = None
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class RecipeInput:
    title: str
    tldr: str | None = None
    description: str | None = None
    tier: str | None = None
    base_servings: str = "4"
    servings_text: str | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    active_minutes: int | None = None
    elapsed_minutes: int | None = None
    source_type: str = "manual"
    source_url: str | None = None
    source_name: str | None = None
    ingredients: list[IngredientInput] = field(default_factory=list)
    steps: list[StepInput] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Output models (what a view/sheet consumes)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngredientView:
    id: int
    section: str | None
    original_text: str
    quantity_text: str | None
    unit_id: int | None
    unit_name: str | None
    food_id: int | None
    food_name: str | None
    note: str | None
    scaling_mode: str
    package_quantity_text: str | None
    package_unit_id: int | None


@dataclass(frozen=True, slots=True)
class StepView:
    id: int
    sort_order: int
    section: str | None
    instruction: str
    minutes: int | None


@dataclass(frozen=True, slots=True)
class RecipeDetail:
    id: int
    slug: str
    title: str
    status: str
    tier: str | None
    tldr: str | None
    description: str | None
    base_servings: str
    servings_text: str | None
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None
    active_minutes: int | None
    elapsed_minutes: int | None
    source_type: str
    source_url: str | None
    source_name: str | None
    created_at: str
    updated_at: str
    ingredients: tuple[IngredientView, ...]
    steps: tuple[StepView, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecipeSummary:
    id: int
    slug: str
    title: str
    status: str
    tier: str | None
    tldr: str | None
    updated_at: str


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _last_id(cur: sqlite3.Cursor) -> int:
    assert cur.lastrowid is not None  # a successful INSERT always yields a rowid
    return cur.lastrowid


def _slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return base or "recipe"


def _unique_slug(conn: sqlite3.Connection, title: str) -> str:
    base = _slugify(title)
    slug = base
    counter = 2
    while conn.execute("SELECT 1 FROM recipes WHERE slug = ?", (slug,)).fetchone() is not None:
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _resolve_food_id(conn: sqlite3.Connection, name: str | None) -> int | None:
    key = _clean(name)
    if key is None:
        return None
    row = conn.execute("SELECT food_id FROM food_aliases WHERE alias = ?", (key,)).fetchone()
    if row is not None:
        return int(row["food_id"])
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (key,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO foods (name, status) VALUES (?, 'confirmed')", (key,))
    return _last_id(cur)


def _compose_original(ing: IngredientInput) -> str:
    if ing.original_text and ing.original_text.strip():
        return ing.original_text.strip()
    parts = [p.strip() for p in (ing.quantity_text, ing.unit, ing.food) if p and p.strip()]
    text = " ".join(parts)
    note = _clean(ing.note)
    if note:
        text = f"{text}, {note}" if text else note
    return text or "(ingredient)"


def _validate(conn: sqlite3.Connection, data: RecipeInput) -> None:
    """Validate the whole recipe before any write, so failures never leave partial rows."""
    if not data.title.strip():
        raise RecipeError("a recipe needs a title")
    if data.tier is not None and data.tier not in VALID_TIER:
        raise RecipeError(f"unknown tier: {data.tier!r}")
    if data.source_type not in VALID_SOURCE_TYPE:
        raise RecipeError(f"unknown source type: {data.source_type!r}")
    if quantity.parse_quantity(data.base_servings) <= 0:
        raise RecipeError("base servings must be greater than zero")
    for ing in data.ingredients:
        label = _compose_original(ing)
        if ing.scaling_mode not in quantity.VALID_SCALING_MODES:
            raise RecipeError(f"unknown scaling mode: {ing.scaling_mode!r}")
        qty = _clean(ing.quantity_text)
        if qty is not None:
            quantity.parse_quantity(qty)
            unit_text = _clean(ing.unit)
            if unit_text is None or units.resolve_unit(conn, unit_text) is None:
                raise RecipeError(f"'{label}' has an amount but no recognised unit")
        if ing.scaling_mode == "round_to_package":
            package_qty = _clean(ing.package_quantity_text)
            if package_qty is None:
                raise RecipeError(f"'{label}' uses round-to-package but has no package size")
            quantity.parse_quantity(package_qty)


def _insert_children(conn: sqlite3.Connection, recipe_id: int, data: RecipeInput) -> None:
    for order, ing in enumerate(data.ingredients):
        unit_text = _clean(ing.unit)
        unit_obj = units.resolve_unit(conn, unit_text) if unit_text else None
        package_text = _clean(ing.package_unit)
        package_obj = units.resolve_unit(conn, package_text) if package_text else None
        conn.execute(
            """INSERT INTO recipe_ingredients
               (recipe_id, sort_order, section, original_text, quantity_text, unit_id, food_id,
                note, scaling_mode, package_quantity_text, package_unit_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                recipe_id, order, _clean(ing.section), _compose_original(ing),
                _clean(ing.quantity_text), unit_obj.id if unit_obj else None,
                _resolve_food_id(conn, ing.food), _clean(ing.note), ing.scaling_mode,
                _clean(ing.package_quantity_text), package_obj.id if package_obj else None,
            ),
        )
    for order, step in enumerate(data.steps):
        instruction = step.instruction.strip()
        if not instruction:
            continue
        conn.execute(
            "INSERT INTO recipe_steps (recipe_id, sort_order, section, instruction, minutes) "
            "VALUES (?, ?, ?, ?, ?)",
            (recipe_id, order, _clean(step.section), instruction, step.minutes),
        )
    for raw_tag in data.tags:
        tag = _clean(raw_tag)
        if tag is None:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        tag_row = conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)",
            (recipe_id, int(tag_row["id"])),
        )


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def create_recipe(
    conn: sqlite3.Connection, data: RecipeInput, *, created_by: int | None = None
) -> int:
    _validate(conn, data)
    stamp = now_iso()
    cur = conn.execute(
        """INSERT INTO recipes
           (slug, title, tldr, description, tier, base_servings, servings_text, prep_minutes,
            cook_minutes, total_minutes, active_minutes, elapsed_minutes, source_type, source_url,
            source_name, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            _unique_slug(conn, data.title), data.title.strip(), _clean(data.tldr),
            _clean(data.description), data.tier, data.base_servings.strip(),
            _clean(data.servings_text), data.prep_minutes, data.cook_minutes, data.total_minutes,
            data.active_minutes, data.elapsed_minutes, data.source_type, _clean(data.source_url),
            _clean(data.source_name), created_by, stamp, stamp,
        ),
    )
    recipe_id = _last_id(cur)
    _insert_children(conn, recipe_id, data)
    conn.commit()
    return recipe_id


def update_recipe(
    conn: sqlite3.Connection, recipe_id: int, data: RecipeInput, *, saved_by: int | None = None
) -> bool:
    current = get_recipe(conn, recipe_id)
    if current is None:
        return False
    _validate(conn, data)
    conn.execute(
        "INSERT INTO recipe_revisions (recipe_id, saved_by, payload) VALUES (?, ?, ?)",
        (recipe_id, saved_by, json.dumps(asdict(current))),
    )
    conn.execute(
        """UPDATE recipes SET title = ?, tldr = ?, description = ?, tier = ?, base_servings = ?,
             servings_text = ?, prep_minutes = ?, cook_minutes = ?, total_minutes = ?,
             active_minutes = ?, elapsed_minutes = ?, source_url = ?, source_name = ?,
             updated_at = ? WHERE id = ?""",
        (
            data.title.strip(), _clean(data.tldr), _clean(data.description), data.tier,
            data.base_servings.strip(), _clean(data.servings_text), data.prep_minutes,
            data.cook_minutes, data.total_minutes, data.active_minutes, data.elapsed_minutes,
            _clean(data.source_url), _clean(data.source_name), now_iso(), recipe_id,
        ),
    )
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    conn.execute("DELETE FROM recipe_steps WHERE recipe_id = ?", (recipe_id,))
    conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
    _insert_children(conn, recipe_id, data)
    conn.commit()
    return True


def set_status(conn: sqlite3.Connection, recipe_id: int, status: str) -> bool:
    if status not in VALID_STATUS:
        raise RecipeError(f"unknown status: {status!r}")
    stamp = now_iso()
    if status == "cookbook":
        cur = conn.execute(
            "UPDATE recipes SET status = ?, promoted_at = ?, updated_at = ? WHERE id = ?",
            (status, stamp, stamp, recipe_id),
        )
    else:
        cur = conn.execute(
            "UPDATE recipes SET status = ?, updated_at = ? WHERE id = ?",
            (status, stamp, recipe_id),
        )
    conn.commit()
    return cur.rowcount > 0


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> bool:
    cur = conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------


def _detail_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> RecipeDetail:
    recipe_id = int(row["id"])
    ing_rows = conn.execute(
        """SELECT ri.*, u.name AS unit_name, fo.name AS food_name
           FROM recipe_ingredients ri
           LEFT JOIN units u ON u.id = ri.unit_id
           LEFT JOIN foods fo ON fo.id = ri.food_id
           WHERE ri.recipe_id = ? ORDER BY ri.sort_order""",
        (recipe_id,),
    ).fetchall()
    ingredients = tuple(
        IngredientView(
            id=int(r["id"]), section=r["section"], original_text=r["original_text"],
            quantity_text=r["quantity_text"], unit_id=r["unit_id"], unit_name=r["unit_name"],
            food_id=r["food_id"], food_name=r["food_name"], note=r["note"],
            scaling_mode=r["scaling_mode"], package_quantity_text=r["package_quantity_text"],
            package_unit_id=r["package_unit_id"],
        )
        for r in ing_rows
    )
    step_rows = conn.execute(
        "SELECT * FROM recipe_steps WHERE recipe_id = ? ORDER BY sort_order", (recipe_id,)
    ).fetchall()
    steps = tuple(
        StepView(
            id=int(r["id"]), sort_order=int(r["sort_order"]), section=r["section"],
            instruction=r["instruction"], minutes=r["minutes"],
        )
        for r in step_rows
    )
    tag_rows = conn.execute(
        "SELECT t.name FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id "
        "WHERE rt.recipe_id = ? ORDER BY t.name COLLATE NOCASE",
        (recipe_id,),
    ).fetchall()
    tags = tuple(str(r["name"]) for r in tag_rows)
    return RecipeDetail(
        id=recipe_id, slug=row["slug"], title=row["title"], status=row["status"], tier=row["tier"],
        tldr=row["tldr"], description=row["description"], base_servings=row["base_servings"],
        servings_text=row["servings_text"], prep_minutes=row["prep_minutes"],
        cook_minutes=row["cook_minutes"], total_minutes=row["total_minutes"],
        active_minutes=row["active_minutes"], elapsed_minutes=row["elapsed_minutes"],
        source_type=row["source_type"], source_url=row["source_url"],
        source_name=row["source_name"], created_at=row["created_at"], updated_at=row["updated_at"],
        ingredients=ingredients, steps=steps, tags=tags,
    )


def get_recipe(conn: sqlite3.Connection, recipe_id: int) -> RecipeDetail | None:
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    return _detail_from_row(conn, row) if row else None


def get_recipe_by_slug(conn: sqlite3.Connection, slug: str) -> RecipeDetail | None:
    row = conn.execute("SELECT * FROM recipes WHERE slug = ?", (slug,)).fetchone()
    return _detail_from_row(conn, row) if row else None


def _fts_query(raw: str) -> str:
    tokens = re.findall(r"\w+", raw.lower())
    return " ".join(f'"{token}"' for token in tokens)


def _summary(row: sqlite3.Row) -> RecipeSummary:
    return RecipeSummary(
        id=int(row["id"]), slug=row["slug"], title=row["title"], status=row["status"],
        tier=row["tier"], tldr=row["tldr"], updated_at=row["updated_at"],
    )


def list_recipes(
    conn: sqlite3.Connection, *, status: str | None = None, query: str | None = None
) -> list[RecipeSummary]:
    params: list[str] = []
    if query and query.strip():
        match = _fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT r.id, r.slug, r.title, r.status, r.tier, r.tldr, r.updated_at "
            "FROM recipe_fts f JOIN recipes r ON r.id = f.rowid WHERE recipe_fts MATCH ?"
        )
        params.append(match)
        if status:
            sql += " AND r.status = ?"
            params.append(status)
        sql += " ORDER BY rank"
    else:
        sql = "SELECT id, slug, title, status, tier, tldr, updated_at FROM recipes"
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC"
    return [_summary(r) for r in conn.execute(sql, params).fetchall()]
