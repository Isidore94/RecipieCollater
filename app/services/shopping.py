"""Shopping list (Phase 4d): one always-visible list built from three sources.

Sources (docs/06 section 3): staples at/below threshold, "add missing from this recipe" minus what
the pantry already holds, and manual adds. Aggregation is a pure canonical-integer merge - amounts
for the same (food, dimension) sum in mg/uL/milli-each (never float); incompatible dimensions stay
as separate lines because we only bridge through an explicit food conversion (none seeded yet).
Regeneration preserves manual lines and check state; it never replaces the active list wholesale.
Lines are grouped by aisle from foods.category (Tandoor's most-loved shopping feature).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.security import now_iso
from app.services import quantity, recipes, units

_OTHER_AISLE = "Other"


class ShoppingError(ValueError):
    """Invalid shopping input."""


@dataclass(frozen=True, slots=True)
class ShoppingItem:
    id: int
    food_id: int | None
    display_text: str
    quantity_text: str | None
    unit_id: int | None
    unit_name: str | None
    canonical_quantity: int | None
    category: str
    is_manual: bool
    checked: bool

    @property
    def label(self) -> str:
        if self.quantity_text and self.unit_name:
            return f"{self.quantity_text} {self.unit_name} {self.display_text}"
        if self.quantity_text:
            return f"{self.quantity_text} {self.display_text}"
        return self.display_text


def active_list(conn: sqlite3.Connection, *, commit: bool = True) -> int:
    """The single active shopping list, created on first use."""
    row = conn.execute(
        "SELECT id FROM shopping_lists WHERE status = 'active' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO shopping_lists (name) VALUES ('Shopping')")
    if commit:
        conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _food_category(conn: sqlite3.Connection, food_id: int | None) -> str:
    if food_id is None:
        return _OTHER_AISLE
    row = conn.execute("SELECT category FROM foods WHERE id = ?", (food_id,)).fetchone()
    return (row["category"] if row and row["category"] else _OTHER_AISLE) or _OTHER_AISLE


def _dimension(conn: sqlite3.Connection, unit_id: int | None) -> str | None:
    if unit_id is None:
        return None
    unit = units.get_unit(conn, unit_id)
    return unit.dimension if unit else None


def _to_item(row: sqlite3.Row) -> ShoppingItem:
    return ShoppingItem(
        id=int(row["id"]), food_id=row["food_id"], display_text=row["display_text"],
        quantity_text=row["quantity_text"], unit_id=row["unit_id"], unit_name=row["unit_name"],
        canonical_quantity=row["canonical_quantity"], category=row["category"] or _OTHER_AISLE,
        is_manual=bool(row["is_manual"]), checked=bool(row["checked"]),
    )


_ITEM_SELECT = """
    SELECT sli.*, u.name AS unit_name
    FROM shopping_list_items sli
    LEFT JOIN units u ON u.id = sli.unit_id
    WHERE sli.list_id = ?
"""


def list_items(conn: sqlite3.Connection, list_id: int) -> list[ShoppingItem]:
    rows = conn.execute(
        _ITEM_SELECT + " ORDER BY sli.checked, sli.category, sli.display_text COLLATE NOCASE",
        (list_id,),
    ).fetchall()
    return [_to_item(r) for r in rows]


def grouped(conn: sqlite3.Connection, list_id: int) -> list[tuple[str, list[ShoppingItem]]]:
    """Items grouped by aisle, unchecked aisles first, each aisle name-sorted."""
    aisles: dict[str, list[ShoppingItem]] = {}
    for item in list_items(conn, list_id):
        aisles.setdefault(item.category, []).append(item)
    ordered = sorted(aisles.items(), key=lambda kv: (kv[0] == _OTHER_AISLE, kv[0].lower()))
    return ordered


# --------------------------------------------------------------------------------------
# Adding to the list
# --------------------------------------------------------------------------------------


def _merge_target(
    conn: sqlite3.Connection, list_id: int, food_id: int | None, dimension: str | None
) -> ShoppingItem | None:
    """An existing unchecked line for the same food + dimension, to sum into (never manual)."""
    if food_id is None:
        return None
    for item in list_items(conn, list_id):
        if (
            item.food_id == food_id
            and not item.is_manual
            and not item.checked
            and _dimension(conn, item.unit_id) == dimension
        ):
            return item
    return None


def _add_measured(
    conn: sqlite3.Connection,
    list_id: int,
    *,
    food_id: int | None,
    display_text: str,
    quantity_text: str | None,
    unit_id: int | None,
    canonical: int | None,
    source_type: str,
    recipe_id: int | None = None,
    label: str | None = None,
) -> int:
    """Insert or merge one measured/aggregatable line, recording its provenance."""
    dimension = _dimension(conn, unit_id)
    target = _merge_target(conn, list_id, food_id, dimension) if canonical is not None else None
    if target is not None and target.canonical_quantity is not None and canonical is not None:
        new_canonical = target.canonical_quantity + canonical
        new_text = quantity.format_quantity(
            quantity.from_canonical(new_canonical, _unit_factor(conn, target.unit_id))
        )
        conn.execute(
            "UPDATE shopping_list_items SET quantity_text = ?, canonical_quantity = ? WHERE id = ?",
            (new_text, new_canonical, target.id),
        )
        item_id = target.id
    else:
        category = _food_category(conn, food_id)
        cur = conn.execute(
            """INSERT INTO shopping_list_items
               (list_id, food_id, display_text, quantity_text, unit_id, canonical_quantity,
                category, is_manual)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (list_id, food_id, display_text, quantity_text, unit_id, canonical, category),
        )
        item_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        """INSERT INTO shopping_item_sources
           (item_id, source_type, recipe_id, quantity_text, unit_id, label)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (item_id, source_type, recipe_id, quantity_text, unit_id, label),
    )
    return item_id


def _unit_factor(conn: sqlite3.Connection, unit_id: int | None) -> int:
    unit = units.get_unit(conn, unit_id) if unit_id else None
    if unit is None or unit.to_canonical_microunits is None:
        raise ShoppingError("cannot re-express a merged amount without a canonical unit")
    return unit.to_canonical_microunits


def add_manual(
    conn: sqlite3.Connection, list_id: int, text: str, *, commit: bool = True
) -> int:
    """Add a free-text line the user typed. Manual lines are never merged or auto-removed."""
    clean = text.strip()
    if not clean:
        raise ShoppingError("nothing to add")
    cur = conn.execute(
        """INSERT INTO shopping_list_items (list_id, display_text, category, is_manual)
           VALUES (?, ?, ?, 1)""",
        (list_id, clean, _OTHER_AISLE),
    )
    item_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "INSERT INTO shopping_item_sources (item_id, source_type, label) VALUES (?, 'manual', ?)",
        (item_id, clean),
    )
    if commit:
        conn.commit()
    return item_id


def _pantry_on_hand(conn: sqlite3.Connection, food_id: int, dimension: str | None) -> int | None:
    """Exact canonical on hand for a food in a dimension, or None if only gauge/binary items exist.

    None means "we track this but not by exact count" - callers treat a present non-empty gauge/
    binary item as 'have enough' and skip it.
    """
    rows = conn.execute(
        "SELECT quantity_mode, canonical_quantity, unit_id, gauge, have "
        "FROM pantry_items WHERE food_id = ?",
        (food_id,),
    ).fetchall()
    if not rows:
        return 0
    exact_total = 0
    saw_exact = False
    for r in rows:
        if r["quantity_mode"] == "exact" and _dimension(conn, r["unit_id"]) == dimension:
            saw_exact = True
            exact_total += r["canonical_quantity"] or 0
        elif r["quantity_mode"] == "gauge" and r["gauge"] != "out":
            return None  # have some (not out) -> treat as covered
        elif r["quantity_mode"] == "binary" and r["have"]:
            return None
    return exact_total if saw_exact else 0


def add_from_recipe(
    conn: sqlite3.Connection,
    list_id: int,
    recipe_id: int,
    *,
    servings: str | None = None,
    missing_only: bool = True,
    commit: bool = True,
) -> int:
    """Add a recipe's measurable ingredients, minus pantry on hand when missing_only.

    Returns the number of lines added.
    """
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        raise ShoppingError("recipe not found")
    factor = recipes.scale_factor(detail.base_servings, servings or detail.base_servings)
    added = 0
    for ing in detail.ingredients:
        if (
            ing.quantity_text is None
            or ing.unit_id is None
            or ing.unit_to_canonical is None
            or ing.scaling_mode in ("fixed", "to_taste")
            or ing.food_id is None
        ):
            continue  # approximate / unmeasured / no food: not aggregatable (kept off the list v1)
        scaled = quantity.parse_quantity(ing.quantity_text) * factor
        needed = quantity.to_canonical(scaled, ing.unit_to_canonical)
        if missing_only:
            on_hand = _pantry_on_hand(conn, ing.food_id, ing.unit_dimension)
            if on_hand is None:
                continue  # a non-exact pantry item covers it
            needed -= on_hand
            if needed <= 0:
                continue
        display_qty = quantity.format_quantity(
            quantity.from_canonical(needed, ing.unit_to_canonical)
        )
        _add_measured(
            conn, list_id, food_id=ing.food_id, display_text=ing.food_name or ing.original_text,
            quantity_text=display_qty, unit_id=ing.unit_id, canonical=needed,
            source_type="recipe", recipe_id=recipe_id, label=ing.original_text,
        )
        added += 1
    if commit:
        conn.commit()
    return added


def add_staples(conn: sqlite3.Connection, list_id: int, *, commit: bool = True) -> int:
    """Add every staple that is out or below its threshold (pantry.shopping_candidates)."""
    from app.services import pantry

    added = 0
    existing = {item.food_id for item in list_items(conn, list_id) if item.food_id is not None}
    for candidate in pantry.shopping_candidates(conn):
        if candidate.food_id is not None and candidate.food_id in existing:
            continue
        cur = conn.execute(
            """INSERT INTO shopping_list_items (list_id, food_id, display_text, category, is_manual)
               VALUES (?, ?, ?, ?, 0)""",
            (list_id, candidate.food_id, candidate.display_name,
             _food_category(conn, candidate.food_id)),
        )
        item_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        conn.execute(
            "INSERT INTO shopping_item_sources (item_id, source_type, label) "
            "VALUES (?, 'staple', ?)",
            (item_id, candidate.display_name),
        )
        added += 1
    if commit:
        conn.commit()
    return added


# --------------------------------------------------------------------------------------
# Mutations + export
# --------------------------------------------------------------------------------------


def toggle(conn: sqlite3.Connection, item_id: int, *, commit: bool = True) -> bool:
    row = conn.execute(
        "SELECT checked FROM shopping_list_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise ShoppingError("unknown item")
    new_checked = 0 if row["checked"] else 1
    conn.execute(
        "UPDATE shopping_list_items SET checked = ?, checked_at = ? WHERE id = ?",
        (new_checked, now_iso() if new_checked else None, item_id),
    )
    if commit:
        conn.commit()
    return bool(new_checked)


def remove(conn: sqlite3.Connection, item_id: int, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM shopping_list_items WHERE id = ?", (item_id,))
    if commit:
        conn.commit()


def clear_checked(conn: sqlite3.Connection, list_id: int, *, commit: bool = True) -> int:
    cur = conn.execute(
        "DELETE FROM shopping_list_items WHERE list_id = ? AND checked = 1", (list_id,)
    )
    if commit:
        conn.commit()
    return cur.rowcount


def to_text(conn: sqlite3.Connection, list_id: int) -> str:
    """Aisle-grouped plain text for copy / share / an Apple Shortcut import."""
    lines: list[str] = []
    for aisle, items in grouped(conn, list_id):
        active = [i for i in items if not i.checked]
        if not active:
            continue
        lines.append(f"{aisle}:")
        lines.extend(f"  - {i.label}" for i in active)
    return "\n".join(lines)


def to_json(conn: sqlite3.Connection, list_id: int) -> dict[str, object]:
    return {
        "aisles": [
            {
                "aisle": aisle,
                "items": [
                    {"text": i.label, "checked": i.checked, "food_id": i.food_id}
                    for i in items
                ],
            }
            for aisle, items in grouped(conn, list_id)
        ]
    }


def counts(conn: sqlite3.Connection, list_id: int) -> tuple[int, int]:
    """(remaining, total) for the list, for a nav badge."""
    items = list_items(conn, list_id)
    return sum(1 for i in items if not i.checked), len(items)
