"""Food ontology upkeep (Phase 4.6): purchase info, aisles, grouping, merge, and learned subs.

Everything in the pantry-aware machinery joins on food_id - coverage, deductions, shopping
aggregation, "use it up". That makes food hygiene load-bearing: duplicate foods ("chicken breast"
vs "chicken breasts") silently break matching. This service is the one place that mutates foods:

- purchase info ("flour comes in 2 kg bags") drives the shopping list's ceiling-to-package display;
- category doubles as the shopping aisle (004), editable here because nothing else sets it;
- parent_food_id groups variants under a family so discovery can match "chicken" to any cut;
- merge_foods collapses a duplicate into its canonical food and rewrites every reference;
- food_substitutes is the household's learned "we used X instead" memory, fed by real cooks.

All amount strings validate through app.services.quantity (CONVENTIONS 1).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.security import now_iso
from app.services import quantity, units


class FoodError(ValueError):
    """Invalid food operation (unknown food, bad amount, or a merge/parent cycle)."""


@dataclass(frozen=True, slots=True)
class PurchaseInfo:
    """How the household buys one package of a food ('1 bag of 2 kg')."""

    quantity_text: str
    unit_id: int | None
    unit_name: str | None
    unit_factor: int | None  # to_canonical_microunits, None for approximate units
    unit_dimension: str | None
    label: str | None  # 'bag', 'can', 'bunch' - the human word for one package

    @property
    def canonical(self) -> int | None:
        """One package in canonical micro-units, when the unit converts exactly."""
        if self.unit_factor is None:
            return None
        return quantity.to_canonical(quantity.parse_quantity(self.quantity_text), self.unit_factor)

    def package_word(self, count: int) -> str:
        """'bag' / 'bags' (or 'pack'/'packs' fallback) for a package count."""
        word = self.label or "pack"
        return word if count == 1 else word + "s"


@dataclass(frozen=True, slots=True)
class FoodInfo:
    id: int
    name: str
    category: str | None
    status: str
    parent_food_id: int | None
    parent_name: str | None
    purchase_quantity_text: str | None
    purchase_unit_name: str | None
    purchase_label: str | None
    recipe_count: int
    pantry_count: int


@dataclass(frozen=True, slots=True)
class SubstituteOption:
    substitute_text: str
    substitute_food_id: int | None
    times_used: int


def _exists(conn: sqlite3.Connection, food_id: int) -> None:
    if conn.execute("SELECT 1 FROM foods WHERE id = ?", (food_id,)).fetchone() is None:
        raise FoodError("unknown food")


# --------------------------------------------------------------------------------------
# Purchase info (what the shopping list ceilings to)
# --------------------------------------------------------------------------------------


def get_purchase(conn: sqlite3.Connection, food_id: int | None) -> PurchaseInfo | None:
    if food_id is None:
        return None
    row = conn.execute(
        """SELECT f.purchase_quantity_text, f.purchase_unit_id, f.purchase_label,
                  u.name AS unit_name, u.dimension, u.to_canonical_microunits
           FROM foods f LEFT JOIN units u ON u.id = f.purchase_unit_id
           WHERE f.id = ?""",
        (food_id,),
    ).fetchone()
    if row is None or (row["purchase_quantity_text"] is None and row["purchase_label"] is None):
        return None
    return PurchaseInfo(
        quantity_text=row["purchase_quantity_text"] or "1",
        unit_id=row["purchase_unit_id"],
        unit_name=row["unit_name"],
        unit_factor=row["to_canonical_microunits"],
        unit_dimension=row["dimension"],
        label=row["purchase_label"],
    )


def set_purchase(
    conn: sqlite3.Connection,
    food_id: int,
    *,
    quantity_text: str | None,
    unit: str | None,
    label: str | None,
    commit: bool = True,
) -> None:
    """Set (or clear, with all-blank input) how one package of this food is bought."""
    _exists(conn, food_id)
    qty = quantity_text.strip() if quantity_text else None
    unit_text = unit.strip() if unit else None
    clean_label = label.strip() if label else None
    unit_id: int | None = None
    if qty:
        if quantity.parse_quantity(qty) <= 0:
            raise FoodError("package size must be greater than zero")
        if unit_text:
            resolved = units.resolve_unit(conn, unit_text)
            if resolved is None:
                raise FoodError(f"unrecognised package unit: {unit_text!r}")
            unit_id = resolved.id
    else:
        qty = None
        unit_id = None
    conn.execute(
        "UPDATE foods SET purchase_quantity_text = ?, purchase_unit_id = ?, purchase_label = ? "
        "WHERE id = ?",
        (qty, unit_id, clean_label, food_id),
    )
    if commit:
        conn.commit()


# --------------------------------------------------------------------------------------
# Category (aisle), status, parent
# --------------------------------------------------------------------------------------


def set_category(
    conn: sqlite3.Connection, food_id: int, category: str | None, *, commit: bool = True
) -> None:
    _exists(conn, food_id)
    clean = category.strip() if category else None
    conn.execute("UPDATE foods SET category = ? WHERE id = ?", (clean, food_id))
    if commit:
        conn.commit()


def confirm_food(conn: sqlite3.Connection, food_id: int, *, commit: bool = True) -> None:
    _exists(conn, food_id)
    conn.execute("UPDATE foods SET status = 'confirmed' WHERE id = ?", (food_id,))
    if commit:
        conn.commit()


def set_parent(
    conn: sqlite3.Connection, food_id: int, parent_food_id: int | None, *, commit: bool = True
) -> None:
    """Group a food under a family ('chicken breast' -> 'chicken'). Guards self/cycles."""
    _exists(conn, food_id)
    if parent_food_id is not None:
        _exists(conn, parent_food_id)
        seen = {food_id}
        cursor: int | None = parent_food_id
        while cursor is not None:
            if cursor in seen:
                raise FoodError("that parent would create a cycle")
            seen.add(cursor)
            row = conn.execute(
                "SELECT parent_food_id FROM foods WHERE id = ?", (cursor,)
            ).fetchone()
            cursor = row["parent_food_id"] if row else None
    conn.execute("UPDATE foods SET parent_food_id = ? WHERE id = ?", (parent_food_id, food_id))
    if commit:
        conn.commit()


def family_ids(conn: sqlite3.Connection, food_id: int) -> set[int]:
    """The food plus its parent, siblings, and children - the match set for discovery."""
    row = conn.execute("SELECT parent_food_id FROM foods WHERE id = ?", (food_id,)).fetchone()
    if row is None:
        return {food_id}
    root = row["parent_food_id"] or food_id
    members = {food_id, int(root)}
    for r in conn.execute("SELECT id FROM foods WHERE parent_food_id = ?", (root,)).fetchall():
        members.add(int(r["id"]))
    return members


# --------------------------------------------------------------------------------------
# Merge (collapse a duplicate food into its canonical one)
# --------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MergeResult:
    """What a merge did, so the caller can describe it and offer to undo it."""

    merge_id: int
    source_name: str
    target_name: str


_REFERENCE_UPDATES: tuple[tuple[str, str], ...] = (
    ("recipe_ingredients", "food_id"),
    ("pantry_items", "food_id"),
    ("pantry_adjustments", "food_id"),
    ("shopping_list_items", "food_id"),
    ("cook_log_ingredients", "food_id"),
    ("foods", "parent_food_id"),
    ("food_substitutes", "substitute_food_id"),
)


def merge_foods(
    conn: sqlite3.Connection, source_id: int, target_id: int, *,
    merged_by: int | None = None, commit: bool = True,
) -> MergeResult:
    """Rewrite every reference from source to target, alias the source name, drop the source.

    The source's aliases (and its own name) become aliases of the target, so the next import of
    "chicken breasts" resolves straight to the canonical food.

    Returns a receipt recording exactly which rows moved, which is the only way this can be
    undone: afterwards nothing distinguishes the target's own references from inherited ones.
    """
    if source_id == target_id:
        raise FoodError("cannot merge a food into itself")
    _exists(conn, source_id)
    _exists(conn, target_id)
    source = conn.execute("SELECT * FROM foods WHERE id = ?", (source_id,)).fetchone()
    target = conn.execute("SELECT name FROM foods WHERE id = ?", (target_id,)).fetchone()

    # Capture what is about to move, before it moves.
    moved: dict[str, list[int]] = {}
    for table, column in _REFERENCE_UPDATES:
        rows = conn.execute(
            f"SELECT rowid AS rid FROM {table} WHERE {column} = ?",  # noqa: S608 - fixed ids
            (source_id,),
        ).fetchall()
        if rows:
            moved[f"{table}.{column}"] = [int(r["rid"]) for r in rows]
    moved_aliases = [
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM food_aliases WHERE food_id = ?", (source_id,)
        ).fetchall()
    ]
    receipt = conn.execute(
        "INSERT INTO food_merges (source_name, target_id, target_name, payload, merged_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            source["name"], target_id, target["name"] if target else "",
            json.dumps({
                "source": dict(source),
                "moved": moved,
                "aliases": moved_aliases,
            }),
            merged_by,
        ),
    )
    receipt_id = int(receipt.lastrowid or 0)

    for table, column in _REFERENCE_UPDATES:
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",  # noqa: S608 - fixed identifiers
            (target_id, source_id),
        )
    # food_substitutes.food_id carries a UNIQUE(food_id, substitute_text); move what fits,
    # drop the duplicates that already exist on the target.
    conn.execute(
        "UPDATE OR IGNORE food_substitutes SET food_id = ? WHERE food_id = ?",
        (target_id, source_id),
    )
    conn.execute("DELETE FROM food_substitutes WHERE food_id = ?", (source_id,))
    # Move aliases (OR IGNORE: the target may already claim one), then alias the source's name.
    conn.execute(
        "UPDATE OR IGNORE food_aliases SET food_id = ? WHERE food_id = ?", (target_id, source_id)
    )
    conn.execute("DELETE FROM food_aliases WHERE food_id = ?", (source_id,))
    conn.execute(
        "INSERT OR IGNORE INTO food_aliases (alias, food_id) VALUES (?, ?)",
        (source["name"], target_id),
    )
    # Carry useful metadata the target lacks (never overwrite what the target already has).
    conn.execute(
        """UPDATE foods SET
             category = COALESCE(category, ?),
             purchase_quantity_text = COALESCE(purchase_quantity_text, ?),
             purchase_unit_id = COALESCE(purchase_unit_id, ?),
             purchase_label = COALESCE(purchase_label, ?)
           WHERE id = ?""",
        (
            source["category"], source["purchase_quantity_text"], source["purchase_unit_id"],
            source["purchase_label"], target_id,
        ),
    )
    conn.execute("DELETE FROM foods WHERE id = ?", (source_id,))
    # Rewriting parent_food_id can leave the target as its own parent (merging a family root
    # into one of its children) or close a longer loop - clear the target's parent if walking
    # up from it ever returns to it, so set_parent's cycle guard stays satisfiable.
    conn.execute(
        "UPDATE foods SET parent_food_id = NULL WHERE id = ? AND parent_food_id = ?",
        (target_id, target_id),
    )
    seen = {target_id}
    cursor = conn.execute(
        "SELECT parent_food_id FROM foods WHERE id = ?", (target_id,)
    ).fetchone()
    walk = cursor["parent_food_id"] if cursor else None
    while walk is not None:
        if walk in seen:
            conn.execute(
                "UPDATE foods SET parent_food_id = NULL WHERE id = ?", (target_id,)
            )
            break
        seen.add(int(walk))
        row = conn.execute("SELECT parent_food_id FROM foods WHERE id = ?", (walk,)).fetchone()
        walk = row["parent_food_id"] if row else None
    if commit:
        conn.commit()
    return MergeResult(
        merge_id=receipt_id,
        source_name=str(source["name"]),
        target_name=str(target["name"]) if target else "",
    )


class MergeUndoUnavailable(FoodError):
    """This merge cannot be reversed (unknown, or already undone)."""


def undo_merge(conn: sqlite3.Connection, merge_id: int, *, commit: bool = True) -> str:
    """Split a merged food back out, returning the name that came back.

    Only the rows the merge actually moved are returned, which is why the receipt records them:
    the target's own references are indistinguishable from inherited ones by the time anyone
    looks. Single-shot, so a replay cannot create a second copy of the food.
    """
    row = conn.execute("SELECT * FROM food_merges WHERE id = ?", (merge_id,)).fetchone()
    if row is None:
        raise MergeUndoUnavailable("that merge is no longer available to undo")
    if row["undone_at"] is not None:
        raise MergeUndoUnavailable("that merge has already been undone")

    payload = json.loads(row["payload"])
    source = payload["source"]
    restored = conn.execute(
        "INSERT INTO foods (name, plural_name, category, density_mg_per_ml, status, "
        "purchase_quantity_text, purchase_unit_id, purchase_label, default_quantity_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source["name"], source.get("plural_name"), source.get("category"),
            source.get("density_mg_per_ml"), source.get("status") or "confirmed",
            source.get("purchase_quantity_text"), source.get("purchase_unit_id"),
            source.get("purchase_label"), source.get("default_quantity_mode"),
        ),
    )
    new_id = int(restored.lastrowid or 0)

    for key, rowids in (payload.get("moved") or {}).items():
        table, _, column = key.partition(".")
        if (table, column) not in _REFERENCE_UPDATES or not rowids:
            continue
        marks = ",".join("?" for _ in rowids)
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE rowid IN ({marks})",  # noqa: S608
            [new_id, *rowids],
        )
    for alias in payload.get("aliases") or []:
        conn.execute(
            "UPDATE OR IGNORE food_aliases SET food_id = ? WHERE alias = ?", (new_id, alias)
        )
    # The merge aliased the source's own name onto the target; that alias is now the food again.
    conn.execute("DELETE FROM food_aliases WHERE alias = ?", (source["name"],))

    conn.execute("UPDATE food_merges SET undone_at = ? WHERE id = ?", (now_iso(), merge_id))
    if commit:
        conn.commit()
    return str(source["name"])


# --------------------------------------------------------------------------------------
# Learned substitutions
# --------------------------------------------------------------------------------------


def record_substitute(
    conn: sqlite3.Connection,
    food_id: int,
    substitute_text: str,
    *,
    source: str = "cook",
    commit: bool = True,
) -> None:
    """Remember "we used X instead of this food" (idempotent; repeat use bumps times_used)."""
    clean = " ".join(substitute_text.split())
    if not clean:
        raise FoodError("nothing to remember")
    _exists(conn, food_id)
    sub_food = conn.execute(
        "SELECT food_id FROM food_aliases WHERE alias = ?", (clean,)
    ).fetchone()
    sub_food_id = (
        int(sub_food["food_id"])
        if sub_food
        else _id_by_name(conn, clean)
    )
    updated = conn.execute(
        "UPDATE food_substitutes SET times_used = times_used + 1 "
        "WHERE food_id = ? AND substitute_text = ? COLLATE NOCASE",
        (food_id, clean),
    )
    if not updated.rowcount:
        conn.execute(
            "INSERT INTO food_substitutes (food_id, substitute_text, substitute_food_id, source) "
            "VALUES (?, ?, ?, ?)",
            (food_id, clean, sub_food_id, source),
        )
    if commit:
        conn.commit()


def _id_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return int(row["id"]) if row else None


def substitutes_for(conn: sqlite3.Connection, food_id: int) -> list[SubstituteOption]:
    rows = conn.execute(
        "SELECT substitute_text, substitute_food_id, times_used FROM food_substitutes "
        "WHERE food_id = ? ORDER BY times_used DESC, substitute_text COLLATE NOCASE",
        (food_id,),
    ).fetchall()
    return [
        SubstituteOption(
            substitute_text=r["substitute_text"],
            substitute_food_id=r["substitute_food_id"],
            times_used=int(r["times_used"]),
        )
        for r in rows
    ]


# --------------------------------------------------------------------------------------
# Listing (the /foods upkeep screen)
# --------------------------------------------------------------------------------------


def list_foods(conn: sqlite3.Connection, *, query: str | None = None) -> list[FoodInfo]:
    """Foods with usage counts, pending-review first, then most-used."""
    pattern = f"%{query.strip()}%" if query and query.strip() else "%"
    rows = conn.execute(
        """SELECT f.id, f.name, f.category, f.status, f.parent_food_id,
                  p.name AS parent_name,
                  f.purchase_quantity_text, f.purchase_label,
                  u.name AS purchase_unit_name,
                  (SELECT COUNT(*) FROM recipe_ingredients ri WHERE ri.food_id = f.id)
                    AS recipe_count,
                  (SELECT COUNT(*) FROM pantry_items pi WHERE pi.food_id = f.id) AS pantry_count
           FROM foods f
           LEFT JOIN foods p ON p.id = f.parent_food_id
           LEFT JOIN units u ON u.id = f.purchase_unit_id
           WHERE f.name LIKE ?
           ORDER BY (f.status = 'pending') DESC, recipe_count + pantry_count DESC,
                    f.name COLLATE NOCASE""",
        (pattern,),
    ).fetchall()
    return [
        FoodInfo(
            id=int(r["id"]), name=r["name"], category=r["category"], status=r["status"],
            parent_food_id=r["parent_food_id"], parent_name=r["parent_name"],
            purchase_quantity_text=r["purchase_quantity_text"],
            purchase_unit_name=r["purchase_unit_name"], purchase_label=r["purchase_label"],
            recipe_count=int(r["recipe_count"]), pantry_count=int(r["pantry_count"]),
        )
        for r in rows
    ]
