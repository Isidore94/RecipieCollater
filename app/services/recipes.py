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
from decimal import Decimal
from urllib.parse import urlsplit

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
    video_seconds: int | None = None  # deep-link into a YouTube recipe at this step


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
    unit_plural: str | None
    food_id: int | None
    food_name: str | None
    note: str | None
    scaling_mode: str
    package_quantity_text: str | None
    package_unit_id: int | None
    # Canonical factors + dimensions carried so pure scaling code can convert a package expressed
    # in a different unit into the ingredient's unit without a DB round-trip (finding #2).
    unit_dimension: str | None = None
    unit_to_canonical: int | None = None
    package_unit_dimension: str | None = None
    package_unit_to_canonical: int | None = None


@dataclass(frozen=True, slots=True)
class StepView:
    id: int
    sort_order: int
    section: str | None
    instruction: str
    minutes: int | None
    video_seconds: int | None


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
    image_path: str | None
    rating: int | None
    notes: str | None
    our_minutes: int | None
    our_active_minutes: int | None
    video_id: str | None
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
    rating: int | None = None
    image_path: str | None = None
    total_minutes: int | None = None
    base_servings: str = "4"


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


def _resolve_food_id(
    conn: sqlite3.Connection, name: str | None, *, food_status: str = "confirmed"
) -> int | None:
    """Resolve a food by alias/name, creating one if new.

    ``food_status`` is the status a NEWLY created food gets: user-typed foods are 'confirmed';
    ingestion passes 'pending' so imported foods go through the review-before-trust flow
    (docs/07 section 3 pending-food chips) instead of silently becoming canonical.
    """
    key = _clean(name)
    if key is None:
        return None
    row = conn.execute("SELECT food_id FROM food_aliases WHERE alias = ?", (key,)).fetchone()
    if row is not None:
        return int(row["food_id"])
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (key,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO foods (name, status) VALUES (?, ?)", (key, food_status))
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
    # A source link is rendered into an href; only http(s) may be stored so a 'javascript:' or
    # 'data:' URL can never become a stored-XSS click target (finding #9.1).
    source_url = _clean(data.source_url)
    if source_url is not None and urlsplit(source_url).scheme.lower() not in ("http", "https"):
        raise RecipeError("source link must be an http:// or https:// URL")
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
            if quantity.parse_quantity(package_qty) <= 0:
                raise RecipeError(f"'{label}' package size must be greater than zero")
            # A package expressed in a different unit ('300 g' bought in '1 kg' packs) must share
            # the ingredient's dimension, or the ceiling-to-package math can't convert it exactly.
            package_unit_text = _clean(ing.package_unit)
            if package_unit_text is not None:
                package_unit = units.resolve_unit(conn, package_unit_text)
                if package_unit is None:
                    raise RecipeError(f"'{label}' has an unrecognised package unit")
                ingredient_unit = units.resolve_unit(conn, _clean(ing.unit) or "")
                if (
                    ingredient_unit is not None
                    and package_unit.dimension != ingredient_unit.dimension
                ):
                    raise RecipeError(
                        f"'{label}' package unit ({package_unit.name}) must measure the same thing "
                        f"as the ingredient's unit ({ingredient_unit.name})"
                    )


def _insert_children(
    conn: sqlite3.Connection, recipe_id: int, data: RecipeInput, *, food_status: str = "confirmed"
) -> None:
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
                _resolve_food_id(conn, ing.food, food_status=food_status), _clean(ing.note),
                ing.scaling_mode,
                _clean(ing.package_quantity_text), package_obj.id if package_obj else None,
            ),
        )
    for order, step in enumerate(data.steps):
        instruction = step.instruction.strip()
        if not instruction:
            continue
        conn.execute(
            "INSERT INTO recipe_steps "
            "(recipe_id, sort_order, section, instruction, minutes, video_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (recipe_id, order, _clean(step.section), instruction, step.minutes, step.video_seconds),
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
    conn: sqlite3.Connection,
    data: RecipeInput,
    *,
    created_by: int | None = None,
    food_status: str = "confirmed",
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
    _insert_children(conn, recipe_id, data, food_status=food_status)
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
    # Ingredient rows are replaced wholesale, but the pantry knowledge earned on them - which
    # pantry item a line deducts from, whether deduction is off, and deduction trust - must
    # survive an edit for lines that didn't change. Otherwise editing a typo in the title
    # silently un-learns every mapping and the next cook re-asks all the questions.
    old_lines = conn.execute(
        """SELECT id, food_id, unit_id, quantity_text, scaling_mode, deduct_from_pantry,
                  pantry_item_hint, deduction_trusted_at, deduction_trust_signature
           FROM recipe_ingredients WHERE recipe_id = ? ORDER BY sort_order""",
        (recipe_id,),
    ).fetchall()
    # Cook-log snapshots reference these ingredient rows (deviations key off ingredient_id, and
    # the FK is ON DELETE SET NULL) - capture the linkage BEFORE the delete nulls it, so
    # matched lines can be re-pointed and a recorded "left it out" still guards its deduction.
    cook_refs = conn.execute(
        """SELECT cli.id AS cli_id, cli.ingredient_id
           FROM cook_log_ingredients cli
           JOIN recipe_ingredients ri ON ri.id = cli.ingredient_id
           WHERE ri.recipe_id = ?""",
        (recipe_id,),
    ).fetchall()
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    conn.execute("DELETE FROM recipe_steps WHERE recipe_id = ?", (recipe_id,))
    conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
    _insert_children(conn, recipe_id, data)
    _carry_over_pantry_knowledge(conn, recipe_id, old_lines, cook_refs)
    conn.commit()
    return True


def _carry_over_pantry_knowledge(
    conn: sqlite3.Connection,
    recipe_id: int,
    old_lines: list[sqlite3.Row],
    cook_refs: list[sqlite3.Row],
) -> None:
    """Copy hint/deduction/trust onto re-inserted lines whose deduction-relevant fields are
    unchanged, and re-point cook-log snapshots at the matched new rows. Trust signatures hash
    (food, unit, quantity, scaling, hint), so an exactly-matched line keeps a still-valid
    signature; any real change misses the match and trust stays revoked.
    """
    consumed: set[int] = set()
    id_map: dict[int, int] = {}  # old ingredient id -> new ingredient id
    new_rows = conn.execute(
        "SELECT id, food_id, unit_id, quantity_text, scaling_mode FROM recipe_ingredients "
        "WHERE recipe_id = ? ORDER BY sort_order",
        (recipe_id,),
    ).fetchall()
    for new in new_rows:
        for index, old in enumerate(old_lines):
            if index in consumed:
                continue
            if (
                old["food_id"] == new["food_id"]
                and old["unit_id"] == new["unit_id"]
                and old["quantity_text"] == new["quantity_text"]
                and old["scaling_mode"] == new["scaling_mode"]
            ):
                consumed.add(index)
                id_map[int(old["id"])] = int(new["id"])
                conn.execute(
                    """UPDATE recipe_ingredients
                       SET deduct_from_pantry = ?, pantry_item_hint = ?,
                           deduction_trusted_at = ?, deduction_trust_signature = ?
                       WHERE id = ?""",
                    (
                        old["deduct_from_pantry"], old["pantry_item_hint"],
                        old["deduction_trusted_at"], old["deduction_trust_signature"],
                        int(new["id"]),
                    ),
                )
                break
    for ref in cook_refs:
        new_id = id_map.get(int(ref["ingredient_id"]))
        if new_id is not None:
            conn.execute(
                "UPDATE cook_log_ingredients SET ingredient_id = ? WHERE id = ?",
                (new_id, int(ref["cli_id"])),
            )


def add_tags(
    conn: sqlite3.Connection, recipe_id: int, tags: list[str], *, commit: bool = True
) -> int:
    """Attach tags to an existing recipe (created if new); returns how many were linked."""
    added = 0
    for raw_tag in tags:
        tag = _clean(raw_tag)
        if tag is None:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        tag_row = conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)
        ).fetchone()
        cur = conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)",
            (recipe_id, int(tag_row["id"])),
        )
        added += cur.rowcount
    if commit:
        conn.commit()
    return added


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


def set_rating(conn: sqlite3.Connection, recipe_id: int, rating: int | None) -> None:
    """Set a 1-10 rating; 0 or None clears it. Out-of-range values are clamped."""
    value = None if not rating else max(1, min(10, int(rating)))
    conn.execute(
        "UPDATE recipes SET rating = ?, updated_at = ? WHERE id = ?",
        (value, now_iso(), recipe_id),
    )
    conn.commit()


def set_notes(conn: sqlite3.Connection, recipe_id: int, notes: str | None) -> None:
    """Store the cook's free-text notes for this recipe (blank clears them)."""
    conn.execute(
        "UPDATE recipes SET notes = ?, updated_at = ? WHERE id = ?",
        (_clean(notes), now_iso(), recipe_id),
    )
    conn.commit()


def set_image(conn: sqlite3.Connection, recipe_id: int, image_path: str) -> None:
    conn.execute(
        "UPDATE recipes SET image_path = ?, updated_at = ? WHERE id = ?",
        (image_path, now_iso(), recipe_id),
    )
    conn.commit()


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------


def _detail_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> RecipeDetail:
    recipe_id = int(row["id"])
    ing_rows = conn.execute(
        """SELECT ri.*, u.name AS unit_name, u.plural_name AS unit_plural,
                  u.dimension AS unit_dimension, u.to_canonical_microunits AS unit_to_canonical,
                  pu.dimension AS package_unit_dimension,
                  pu.to_canonical_microunits AS package_unit_to_canonical,
                  fo.name AS food_name
           FROM recipe_ingredients ri
           LEFT JOIN units u ON u.id = ri.unit_id
           LEFT JOIN units pu ON pu.id = ri.package_unit_id
           LEFT JOIN foods fo ON fo.id = ri.food_id
           WHERE ri.recipe_id = ? ORDER BY ri.sort_order""",
        (recipe_id,),
    ).fetchall()
    ingredients = tuple(
        IngredientView(
            id=int(r["id"]), section=r["section"], original_text=r["original_text"],
            quantity_text=r["quantity_text"], unit_id=r["unit_id"], unit_name=r["unit_name"],
            unit_plural=r["unit_plural"], food_id=r["food_id"], food_name=r["food_name"],
            note=r["note"],
            scaling_mode=r["scaling_mode"], package_quantity_text=r["package_quantity_text"],
            package_unit_id=r["package_unit_id"],
            unit_dimension=r["unit_dimension"], unit_to_canonical=r["unit_to_canonical"],
            package_unit_dimension=r["package_unit_dimension"],
            package_unit_to_canonical=r["package_unit_to_canonical"],
        )
        for r in ing_rows
    )
    step_rows = conn.execute(
        "SELECT * FROM recipe_steps WHERE recipe_id = ? ORDER BY sort_order", (recipe_id,)
    ).fetchall()
    steps = tuple(
        StepView(
            id=int(r["id"]), sort_order=int(r["sort_order"]), section=r["section"],
            instruction=r["instruction"], minutes=r["minutes"], video_seconds=r["video_seconds"],
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
        source_name=row["source_name"], image_path=row["image_path"],
        rating=row["rating"], notes=row["notes"],
        our_minutes=row["our_minutes"], our_active_minutes=row["our_active_minutes"],
        video_id=row["video_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
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
        rating=row["rating"], image_path=row["image_path"],
        total_minutes=row["total_minutes"], base_servings=row["base_servings"],
    )


# Kept as two pure literals (not an f-string build) so ruff's SQL-injection check can see
# that user input only ever travels through bound params.
_SUMMARY_SELECT_FTS = (
    "SELECT r.id, r.slug, r.title, r.status, r.tier, r.tldr, r.updated_at, r.rating, "
    "r.image_path, r.total_minutes, r.base_servings "
    "FROM recipe_fts f JOIN recipes r ON r.id = f.rowid WHERE recipe_fts MATCH ?"
)
_SUMMARY_SELECT_PLAIN = (
    "SELECT r.id, r.slug, r.title, r.status, r.tier, r.tldr, r.updated_at, r.rating, "
    "r.image_path, r.total_minutes, r.base_servings FROM recipes r"
)


def list_recipes(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    query: str | None = None,
    tag: str | None = None,
    tier: str | None = None,
    max_minutes: int | None = None,
    min_rating: int | None = None,
) -> list[RecipeSummary]:
    """Search + filter the library. Filters compose with each other and with FTS search
    (docs/07 section 2: tier / tags / max time / rating filters on the Cookbook)."""
    params: list[str | int] = []
    where: list[str] = []
    if status:
        where.append("r.status = ?")
        params.append(status)
    if tag and tag.strip():
        where.append(
            "EXISTS (SELECT 1 FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id "
            "WHERE rt.recipe_id = r.id AND t.name = ? COLLATE NOCASE)"
        )
        params.append(tag.strip())
    if tier and tier in VALID_TIER:
        where.append("r.tier = ?")
        params.append(tier)
    if max_minutes is not None and max_minutes > 0:
        where.append("COALESCE(r.our_minutes, r.total_minutes) <= ?")
        params.append(max_minutes)
    if min_rating is not None and min_rating > 0:
        where.append("r.rating >= ?")
        params.append(min_rating)

    if query and query.strip():
        match = _fts_query(query)
        if not match:
            return []
        sql = _SUMMARY_SELECT_FTS
        params.insert(0, match)
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY rank"
    else:
        sql = _SUMMARY_SELECT_PLAIN
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.updated_at DESC, r.id DESC"
    return [_summary(r) for r in conn.execute(sql, params).fetchall()]


@dataclass(frozen=True, slots=True)
class TagCount:
    name: str
    count: int


def list_tags(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 12
) -> list[TagCount]:
    """The most-used tags (for the Cookbook filter chips)."""
    params: list[str | int] = []
    sql = (
        "SELECT t.name, COUNT(*) AS n FROM tags t "
        "JOIN recipe_tags rt ON rt.tag_id = t.id JOIN recipes r ON r.id = rt.recipe_id "
    )
    if status:
        sql += "WHERE r.status = ? "
        params.append(status)
    sql += "GROUP BY t.id ORDER BY n DESC, t.name COLLATE NOCASE LIMIT ?"
    params.append(limit)
    return [
        TagCount(name=r["name"], count=int(r["n"]))
        for r in conn.execute(sql, params).fetchall()
    ]


# --------------------------------------------------------------------------------------
# Ephemeral serving scaler (server-authoritative; never writes)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScaledIngredient:
    section: str | None
    display: str
    scaling_mode: str


def scale_factor(base_servings: str, target_servings: str) -> Decimal:
    base = quantity.parse_quantity(base_servings)
    target = quantity.parse_quantity(target_servings)
    if base <= 0:
        return Decimal(1)
    return target / base


def package_in_unit(ing: IngredientView) -> Decimal | None:
    """The round_to_package package size expressed in the INGREDIENT's own unit.

    ``package_unit_id`` may differ from the ingredient's unit - a '300 g' ingredient bought in
    '1 kg' packs - so convert through canonical micro-units (finding #2). When the package uses the
    same/absent unit, or either unit is approximate (no factor), no conversion is possible or needed
    and the raw amount is returned. Validation guarantees matching dimensions when a package unit is
    set, so the dimension guard here is purely defensive.
    """
    if not ing.package_quantity_text:
        return None
    pkg = quantity.parse_quantity(ing.package_quantity_text)
    if (
        ing.package_unit_id is None
        or ing.package_unit_id == ing.unit_id
        or ing.unit_to_canonical is None
        or ing.package_unit_to_canonical is None
        or ing.unit_dimension != ing.package_unit_dimension
    ):
        return pkg
    return quantity.convert(pkg, ing.package_unit_to_canonical, ing.unit_to_canonical)


def _scaled_display(ing: IngredientView, factor: Decimal) -> str:
    # No amount, an unscaled view, fixed, and to-taste lines keep the original wording.
    if ing.quantity_text is None or ing.unit_name is None:
        return ing.original_text
    if factor == 1 or ing.scaling_mode in ("fixed", "to_taste"):
        return ing.original_text
    amount = quantity.parse_quantity(ing.quantity_text)
    scaled = quantity.scale(
        amount, factor=factor, mode=ing.scaling_mode, package=package_in_unit(ing)
    )
    if scaled is None:
        return ing.original_text
    unit_label = ing.unit_plural if (scaled > 1 and ing.unit_plural) else ing.unit_name
    parts = [quantity.format_quantity(scaled), unit_label]
    if ing.food_name:
        parts.append(ing.food_name)
    text = " ".join(parts)
    return f"{text}, {ing.note}" if ing.note else text


def scale_ingredients(detail: RecipeDetail, target_servings: str) -> list[ScaledIngredient]:
    """Recompute each ingredient's display line at target servings. Ephemeral; never writes."""
    factor = scale_factor(detail.base_servings, target_servings)
    return [
        ScaledIngredient(
            section=ing.section,
            display=_scaled_display(ing, factor),
            scaling_mode=ing.scaling_mode,
        )
        for ing in detail.ingredients
    ]


def to_markdown(detail: RecipeDetail) -> str:
    """Render a recipe as portable Markdown (export; CONVENTIONS: data stays portable)."""
    lines = [f"# {detail.title}", ""]
    if detail.tldr:
        lines += [detail.tldr, ""]
    lines += ["## Ingredients", ""]
    current_section: str | None = None
    for ing in detail.ingredients:
        if ing.section and ing.section != current_section:
            lines.append(f"**{ing.section}**")
            current_section = ing.section
        lines.append(f"- {ing.original_text}")
    if detail.steps:
        lines += ["", "## Steps", ""]
        for index, step in enumerate(detail.steps, start=1):
            lines.append(f"{index}. {step.instruction}")
    if detail.tags:
        lines += ["", f"_Tags: {', '.join(detail.tags)}_"]
    return "\n".join(lines) + "\n"
