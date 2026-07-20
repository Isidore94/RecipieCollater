"""Shopping list (Phase 4d, rebuilt in Phase 4.6): a list that speaks "store", not "recipe".

Sources (docs/06 section 3): staples at/below threshold, "add missing from this recipe" minus what
the pantry already holds, the multi-recipe trip builder, and manual adds. Aggregation is a pure
canonical-integer merge - amounts for the same (food, dimension) sum in mg/uL/milli-each (never
float); incompatible dimensions stay as separate lines because we only bridge through an explicit
food conversion. Lines are grouped by aisle from foods.category.

Phase 4.6 store-language rules (docs/07 section 3):
- Foods tracked as gauge/binary in the pantry are STAPLE-LANE: recipe amounts never appear on the
  list. Either the pantry covers them (reported, not listed) or they land as a quantity-less line
  rendered in purchase units ("Flour - 1 bag (2 kg)").
- Measured lines ceiling to the food's purchase size at display time ("2 bags (need 3 cups)").
  Aggregation stays canonical; the package math is presentation, recomputed on every render.
- Nothing is silently dropped: unmeasurable lines land flagged as "check the amount".
- fixed-scaling lines DO shop (the amount just doesn't scale with servings); to_taste lines stay
  off the list (salt on every list is noise) but are reported to the caller.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from app.security import now_iso
from app.services import foods, pantry, quantity, recipes, units

_OTHER_AISLE = "Other"


class ShoppingError(ValueError):
    """Invalid shopping input."""


# --------------------------------------------------------------------------------------
# Items + display (the store-language layer)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShoppingItem:
    id: int
    food_id: int | None
    display_text: str
    quantity_text: str | None
    unit_id: int | None
    unit_name: str | None
    unit_dimension: str | None
    canonical_quantity: int | None
    category: str
    is_manual: bool
    checked: bool
    needs_check: bool
    purchase: foods.PurchaseInfo | None

    @property
    def packages(self) -> int | None:
        """How many purchase packages cover the needed amount, when both convert exactly."""
        if (
            self.purchase is None
            or self.canonical_quantity is None
            or self.canonical_quantity <= 0
            or self.purchase.canonical is None
            or self.purchase.canonical <= 0
            or self.purchase.unit_dimension != self.unit_dimension
        ):
            return None
        per_package = Decimal(self.purchase.canonical)
        return int(
            (Decimal(self.canonical_quantity) / per_package)
            .to_integral_value(rounding=ROUND_CEILING)
        )

    @property
    def label(self) -> str:
        if self.needs_check:
            return f"{self.display_text} (check the amount)"
        packages = self.packages
        if packages is not None and self.purchase is not None:
            need = f"{self.quantity_text} {self.unit_name}" if self.unit_name else ""
            word = self.purchase.package_word(packages)
            size = self.purchase.quantity_text
            size_unit = self.purchase.unit_name or ""
            per = f"{size} {size_unit}".strip()
            detail = f"need {need}" if need else ""
            inside = ", ".join(p for p in (per if packages == 1 else "", detail) if p)
            suffix = f" ({inside})" if inside else ""
            return f"{self.display_text} - {packages} {word}{suffix}"
        if self.quantity_text and self.unit_name:
            return f"{self.quantity_text} {self.unit_name} {self.display_text}"
        if self.quantity_text:
            return f"{self.quantity_text} {self.display_text}"
        if self.purchase is not None and self.quantity_text is None and not self.is_manual:
            # Staple-lane line: suggest one package in the household's purchase words.
            per = (
                f"{self.purchase.quantity_text} {self.purchase.unit_name}".strip()
                if self.purchase.unit_name
                else self.purchase.quantity_text
            )
            per_note = f" ({per})" if self.purchase.quantity_text != "1" else ""
            return f"{self.display_text} - 1 {self.purchase.package_word(1)}{per_note}"
        return self.display_text

    @property
    def wants_purchase_info(self) -> bool:
        """Show the one-time "how do you buy this?" prompt for this line."""
        return (
            self.food_id is not None
            and not self.is_manual
            and not self.checked
            and self.purchase is None
        )


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
    purchase: foods.PurchaseInfo | None = None
    if row["p_qty"] is not None or row["p_label"] is not None:
        purchase = foods.PurchaseInfo(
            quantity_text=row["p_qty"] or "1",
            unit_id=row["p_unit_id"],
            unit_name=row["p_unit_name"],
            unit_factor=row["p_factor"],
            unit_dimension=row["p_dim"],
            label=row["p_label"],
        )
    return ShoppingItem(
        id=int(row["id"]), food_id=row["food_id"], display_text=row["display_text"],
        quantity_text=row["quantity_text"], unit_id=row["unit_id"], unit_name=row["unit_name"],
        unit_dimension=row["unit_dimension"],
        canonical_quantity=row["canonical_quantity"], category=row["category"] or _OTHER_AISLE,
        is_manual=bool(row["is_manual"]), checked=bool(row["checked"]),
        needs_check=bool(row["needs_check"]), purchase=purchase,
    )


_ITEM_SELECT = """
    SELECT sli.*, u.name AS unit_name, u.dimension AS unit_dimension,
           f.purchase_quantity_text AS p_qty, f.purchase_label AS p_label,
           f.purchase_unit_id AS p_unit_id,
           pu.name AS p_unit_name, pu.to_canonical_microunits AS p_factor,
           pu.dimension AS p_dim
    FROM shopping_list_items sli
    LEFT JOIN units u ON u.id = sli.unit_id
    LEFT JOIN foods f ON f.id = sli.food_id
    LEFT JOIN units pu ON pu.id = f.purchase_unit_id
    WHERE sli.list_id = ?
"""


def list_items(conn: sqlite3.Connection, list_id: int) -> list[ShoppingItem]:
    rows = conn.execute(
        _ITEM_SELECT + " ORDER BY sli.checked, sli.category, sli.display_text COLLATE NOCASE",
        (list_id,),
    ).fetchall()
    return [_to_item(r) for r in rows]


def grouped(conn: sqlite3.Connection, list_id: int) -> list[tuple[str, list[ShoppingItem]]]:
    """Items grouped by aisle, each aisle name-sorted, 'Other' last."""
    aisles: dict[str, list[ShoppingItem]] = {}
    for item in list_items(conn, list_id):
        aisles.setdefault(item.category, []).append(item)
    ordered = sorted(aisles.items(), key=lambda kv: (kv[0] == _OTHER_AISLE, kv[0].lower()))
    return ordered


def sources_by_item(conn: sqlite3.Connection, list_id: int) -> dict[int, list[str]]:
    """item_id -> provenance labels ("for Chili", "staple") for the list page."""
    rows = conn.execute(
        """SELECT s.item_id, s.source_type, r.title
           FROM shopping_item_sources s
           JOIN shopping_list_items sli ON sli.id = s.item_id
           LEFT JOIN recipes r ON r.id = s.recipe_id
           WHERE sli.list_id = ? ORDER BY s.id""",
        (list_id,),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        if r["source_type"] == "recipe" and r["title"]:
            label = f"for {r['title']}"
        elif r["source_type"] == "staple":
            label = "staple"
        else:
            continue  # manual provenance is self-evident
        bucket = out.setdefault(int(r["item_id"]), [])
        if label not in bucket:
            bucket.append(label)
    return out


# --------------------------------------------------------------------------------------
# Planning one recipe's lines (shared by single-recipe add and the trip builder)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedLine:
    """One recipe ingredient turned into a shopping decision (before any write)."""

    key: str  # 'f:<food_id>' or 'i:<ingredient_id>' - stable across preview/apply
    kind: str  # 'measured' | 'staple' | 'check' | 'covered' | 'to_taste'
    ingredient_id: int
    food_id: int | None
    display_text: str
    original_text: str
    quantity_text: str | None  # needed amount, already pantry-subtracted (measured only)
    unit_id: int | None
    canonical: int | None
    covered_reason: str | None = None


def _pantry_profile(
    conn: sqlite3.Connection, food_id: int, dimension: str | None
) -> tuple[bool, bool, int]:
    """(has gauge/binary items, any gauge/binary non-empty, exact canonical total in dimension)."""
    rows = conn.execute(
        "SELECT quantity_mode, canonical_quantity, unit_id, gauge, have "
        "FROM pantry_items WHERE food_id = ?",
        (food_id,),
    ).fetchall()
    has_gb = False
    gb_nonempty = False
    exact_total = 0
    for r in rows:
        if r["quantity_mode"] == "exact":
            if _dimension(conn, r["unit_id"]) == dimension:
                exact_total += r["canonical_quantity"] or 0
        else:
            has_gb = True
            if (r["quantity_mode"] == "gauge" and r["gauge"] != "out") or (
                r["quantity_mode"] == "binary" and r["have"]
            ):
                gb_nonempty = True
    return has_gb, gb_nonempty, exact_total


def _scaled_need(ing: recipes.IngredientView, factor: Decimal) -> Decimal:
    """The amount to shop for: fixed doesn't scale, round_to_package ceilings to its package."""
    amount = quantity.parse_quantity(ing.quantity_text or "0")
    if ing.scaling_mode == "fixed":
        return amount
    if ing.scaling_mode == "round_to_package":
        scaled = quantity.scale(
            amount, factor=factor, mode="round_to_package", package=recipes.package_in_unit(ing)
        )
        return scaled if scaled is not None else amount * factor
    return amount * factor


def plan_recipe(
    conn: sqlite3.Connection,
    recipe_id: int,
    *,
    servings: str | None = None,
    missing_only: bool = True,
) -> list[PlannedLine]:
    """Turn one recipe into shopping decisions. Nothing is dropped: every ingredient becomes a
    measured/staple/check line or an explained covered/to_taste entry."""
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        raise ShoppingError("recipe not found")
    factor = recipes.scale_factor(detail.base_servings, servings or detail.base_servings)
    lines: list[PlannedLine] = []
    # Pantry stock a previous line of THIS recipe already claimed, per (food, dimension) -
    # a recipe using flour in two lines must not have both lines count the same bag.
    consumed: dict[tuple[int, str | None], int] = {}
    for ing in detail.ingredients:
        name = ing.food_name or ing.original_text
        if ing.scaling_mode == "to_taste":
            lines.append(
                PlannedLine(
                    key=f"i:{ing.id}", kind="to_taste", ingredient_id=ing.id,
                    food_id=ing.food_id, display_text=name, original_text=ing.original_text,
                    quantity_text=None, unit_id=None, canonical=None,
                )
            )
            continue
        measurable = (
            ing.quantity_text is not None
            and ing.unit_id is not None
            and ing.unit_to_canonical is not None
            and ing.food_id is not None
        )
        if not measurable:
            covered_reason = None
            if ing.food_id is not None and missing_only:
                _, gb_nonempty, _ = _pantry_profile(conn, ing.food_id, None)
                if gb_nonempty:
                    covered_reason = "in the pantry"
            lines.append(
                PlannedLine(
                    key=f"i:{ing.id}", kind="covered" if covered_reason else "check",
                    ingredient_id=ing.id, food_id=ing.food_id, display_text=name,
                    original_text=ing.original_text, quantity_text=None, unit_id=None,
                    canonical=None, covered_reason=covered_reason,
                )
            )
            continue
        assert ing.food_id is not None and ing.unit_to_canonical is not None
        needed_amount = _scaled_need(ing, factor)
        needed = quantity.to_canonical(needed_amount, ing.unit_to_canonical)
        has_gb, gb_nonempty, exact_total = _pantry_profile(
            conn, ing.food_id, ing.unit_dimension
        )
        if missing_only:
            if gb_nonempty:
                lines.append(
                    PlannedLine(
                        key=f"f:{ing.food_id}", kind="covered", ingredient_id=ing.id,
                        food_id=ing.food_id, display_text=name, original_text=ing.original_text,
                        quantity_text=None, unit_id=None, canonical=None,
                        covered_reason="in the pantry",
                    )
                )
                continue
            stock_key = (ing.food_id, ing.unit_dimension)
            available = max(0, exact_total - consumed.get(stock_key, 0))
            claimed = min(available, needed)
            consumed[stock_key] = consumed.get(stock_key, 0) + claimed
            needed -= claimed
            if needed <= 0:
                lines.append(
                    PlannedLine(
                        key=f"f:{ing.food_id}", kind="covered", ingredient_id=ing.id,
                        food_id=ing.food_id, display_text=name, original_text=ing.original_text,
                        quantity_text=None, unit_id=None, canonical=None,
                        covered_reason="enough in the pantry",
                    )
                )
                continue
        if has_gb and not gb_nonempty and missing_only:
            # Staple-lane restock: the household tracks this loosely; never list "1/4 cup".
            lines.append(
                PlannedLine(
                    key=f"f:{ing.food_id}", kind="staple", ingredient_id=ing.id,
                    food_id=ing.food_id, display_text=name, original_text=ing.original_text,
                    quantity_text=None, unit_id=None, canonical=None,
                )
            )
            continue
        display_qty = quantity.format_quantity(
            quantity.from_canonical(needed, ing.unit_to_canonical)
        )
        lines.append(
            PlannedLine(
                key=f"f:{ing.food_id}", kind="measured", ingredient_id=ing.id,
                food_id=ing.food_id, display_text=name, original_text=ing.original_text,
                quantity_text=display_qty, unit_id=ing.unit_id, canonical=needed,
            )
        )
    return lines


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
            and not item.needs_check
            and item.unit_dimension == dimension
        ):
            return item
    return None


def _record_source(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    source_type: str,
    recipe_id: int | None = None,
    quantity_text: str | None = None,
    unit_id: int | None = None,
    label: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO shopping_item_sources
           (item_id, source_type, recipe_id, quantity_text, unit_id, label)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (item_id, source_type, recipe_id, quantity_text, unit_id, label),
    )


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
    _record_source(
        conn, item_id, source_type=source_type, recipe_id=recipe_id,
        quantity_text=quantity_text, unit_id=unit_id, label=label,
    )
    return item_id


def _add_quantityless(
    conn: sqlite3.Connection,
    list_id: int,
    *,
    food_id: int | None,
    display_text: str,
    needs_check: bool,
    source_type: str,
    recipe_id: int | None = None,
    label: str | None = None,
) -> int:
    """Insert a staple-lane or check line, deduping against an existing unchecked twin."""
    existing: sqlite3.Row | None = None
    if needs_check:
        existing = conn.execute(
            "SELECT id FROM shopping_list_items WHERE list_id = ? AND checked = 0 "
            "AND is_manual = 0 AND needs_check = 1 AND display_text = ? COLLATE NOCASE",
            (list_id, display_text),
        ).fetchone()
    elif food_id is not None:
        existing = conn.execute(
            "SELECT id FROM shopping_list_items WHERE list_id = ? AND food_id = ? "
            "AND checked = 0 AND is_manual = 0 AND quantity_text IS NULL AND needs_check = 0",
            (list_id, food_id),
        ).fetchone()
    if existing is not None:
        item_id = int(existing["id"])
    else:
        cur = conn.execute(
            """INSERT INTO shopping_list_items
               (list_id, food_id, display_text, category, is_manual, needs_check)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (
                list_id, food_id, display_text, _food_category(conn, food_id),
                1 if needs_check else 0,
            ),
        )
        item_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    _record_source(conn, item_id, source_type=source_type, recipe_id=recipe_id, label=label)
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
    _record_source(conn, item_id, source_type="manual", label=clean)
    if commit:
        conn.commit()
    return item_id


@dataclass(frozen=True, slots=True)
class AddOutcome:
    """What one recipe-add actually did, for an honest notice on the list page."""

    added: int
    covered: list[str] = field(default_factory=list)
    to_check: int = 0

    @property
    def notice(self) -> str:
        parts = [f"Added {self.added} item{'s' if self.added != 1 else ''}"]
        if self.covered:
            parts.append(f"{len(self.covered)} covered by the pantry")
        if self.to_check:
            parts.append(f"{self.to_check} to check")
        return " · ".join(parts)


def _write_line(
    conn: sqlite3.Connection, list_id: int, line: PlannedLine, recipe_id: int
) -> int | None:
    if line.kind == "measured":
        return _add_measured(
            conn, list_id, food_id=line.food_id, display_text=line.display_text,
            quantity_text=line.quantity_text, unit_id=line.unit_id, canonical=line.canonical,
            source_type="recipe", recipe_id=recipe_id, label=line.original_text,
        )
    if line.kind in ("staple", "check"):
        return _add_quantityless(
            conn, list_id, food_id=line.food_id, display_text=line.display_text,
            needs_check=(line.kind == "check"), source_type="recipe", recipe_id=recipe_id,
            label=line.original_text,
        )
    return None


def add_from_recipe(
    conn: sqlite3.Connection,
    list_id: int,
    recipe_id: int,
    *,
    servings: str | None = None,
    missing_only: bool = True,
    commit: bool = True,
) -> AddOutcome:
    """Add a recipe's shopping decisions to the list (see plan_recipe for the rules)."""
    lines = plan_recipe(conn, recipe_id, servings=servings, missing_only=missing_only)
    added = 0
    covered: list[str] = []
    to_check = 0
    for line in lines:
        if line.kind == "covered":
            covered.append(line.display_text)
        elif line.kind == "to_taste":
            continue
        else:
            _write_line(conn, list_id, line, recipe_id)
            added += 1
            if line.kind == "check":
                to_check += 1
    if commit:
        conn.commit()
    return AddOutcome(added=added, covered=covered, to_check=to_check)


def add_staples(conn: sqlite3.Connection, list_id: int, *, commit: bool = True) -> int:
    """Add every staple that is out or below its threshold (pantry.shopping_candidates)."""
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
        _record_source(conn, item_id, source_type="staple", label=candidate.display_name)
        added += 1
    if commit:
        conn.commit()
    return added


# --------------------------------------------------------------------------------------
# The trip builder: several recipes -> one previewed, pantry-aware list
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TripLine:
    key: str
    kind: str  # 'measured' | 'staple' | 'check'
    food_id: int | None
    display_text: str
    original_text: str
    quantity_text: str | None
    unit_id: int | None
    unit_name: str | None
    canonical: int | None
    aisle: str
    recipe_titles: tuple[str, ...]
    purchase_note: str | None  # '2 bags (2 kg each)' when purchase info converts

    @property
    def label(self) -> str:
        if self.kind == "check":
            return f"{self.display_text} (check the amount)"
        if self.purchase_note:
            need = (
                f" (need {self.quantity_text} {self.unit_name})"
                if self.quantity_text and self.unit_name
                else ""
            )
            return f"{self.display_text} - {self.purchase_note}{need}"
        if self.quantity_text and self.unit_name:
            return f"{self.quantity_text} {self.unit_name} {self.display_text}"
        return self.display_text


@dataclass(frozen=True, slots=True)
class TripPreview:
    picks: list[tuple[int, str]]  # (recipe_id, servings)
    to_buy: list[TripLine] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    to_taste: list[str] = field(default_factory=list)

    @property
    def aisles(self) -> list[tuple[str, list[TripLine]]]:
        grouped_lines: dict[str, list[TripLine]] = {}
        for line in self.to_buy:
            grouped_lines.setdefault(line.aisle, []).append(line)
        return sorted(grouped_lines.items(), key=lambda kv: (kv[0] == _OTHER_AISLE, kv[0].lower()))


def _purchase_note(conn: sqlite3.Connection, line: PlannedLine) -> str | None:
    purchase = foods.get_purchase(conn, line.food_id)
    if purchase is None:
        return None
    if line.kind == "staple" or line.canonical is None:
        per = (
            f"{purchase.quantity_text} {purchase.unit_name}".strip()
            if purchase.unit_name
            else purchase.quantity_text
        )
        note = f"1 {purchase.package_word(1)}"
        return f"{note} ({per})" if purchase.quantity_text != "1" else note
    if (
        purchase.canonical is None
        or purchase.canonical <= 0
        or purchase.unit_dimension != _dimension(conn, line.unit_id)
    ):
        return None
    packages = int(
        (Decimal(line.canonical) / Decimal(purchase.canonical))
        .to_integral_value(rounding=ROUND_CEILING)
    )
    return f"{packages} {purchase.package_word(packages)}"


def build_trip(
    conn: sqlite3.Connection, picks: list[tuple[int, str | None]]
) -> TripPreview:
    """Aggregate several recipes into one pantry-aware preview. Pure - never writes.

    Pantry stock is subtracted ONCE against the aggregate need per food (adding two recipes
    one at a time would let both claim the same jar).
    """
    resolved_picks: list[tuple[int, str]] = []
    raw: list[tuple[PlannedLine, str]] = []  # (line pre-subtraction, recipe title)
    for recipe_id, servings in picks:
        detail = recipes.get_recipe(conn, recipe_id)
        if detail is None:
            continue
        target = servings or detail.base_servings
        resolved_picks.append((recipe_id, target))
        for line in plan_recipe(conn, recipe_id, servings=target, missing_only=False):
            raw.append((line, detail.title))

    covered: list[str] = []
    to_taste: list[str] = []
    to_buy: list[TripLine] = []
    # Aggregate measured needs per (food, dimension); collect titles per bucket for provenance.
    totals: dict[tuple[int, str | None], int] = {}
    meta: dict[tuple[int, str | None], PlannedLine] = {}
    bucket_titles: dict[tuple[int, str | None], list[str]] = {}
    check_titles: dict[str, list[str]] = {}
    seen_quantityless: set[str] = set()
    staple_foods_listed: set[int] = set()

    def note(bucket: list[str], title: str) -> None:
        if title not in bucket:
            bucket.append(title)

    for line, title in raw:
        if line.kind == "to_taste":
            if line.display_text not in to_taste:
                to_taste.append(line.display_text)
        elif line.kind == "measured" and line.food_id is not None:
            dim = _dimension(conn, line.unit_id)
            bucket_key = (line.food_id, dim)
            totals[bucket_key] = totals.get(bucket_key, 0) + (line.canonical or 0)
            meta.setdefault(bucket_key, line)
            note(bucket_titles.setdefault(bucket_key, []), title)
        elif line.kind == "check":
            note(check_titles.setdefault(line.key, []), title)

    for (food_id, dim), total in totals.items():
        line = meta[(food_id, dim)]
        titles = bucket_titles.get((food_id, dim), [])
        has_gb, gb_nonempty, exact_total = _pantry_profile(conn, food_id, dim)
        if gb_nonempty:
            covered.append(line.display_text)
            continue
        needed = total - exact_total
        if needed <= 0:
            covered.append(line.display_text)
            continue
        if has_gb:
            # One staple line per food, even when recipes measure it in several dimensions.
            if food_id in staple_foods_listed:
                continue
            staple_foods_listed.add(food_id)
            staple = PlannedLine(
                key=f"f:{food_id}", kind="staple", ingredient_id=line.ingredient_id,
                food_id=food_id, display_text=line.display_text,
                original_text=line.original_text, quantity_text=None, unit_id=None,
                canonical=None,
            )
            to_buy.append(_trip_line(conn, staple, titles))
            continue
        factor = _unit_factor(conn, line.unit_id)
        # The key carries the dimension: one food measured by mass AND volume yields two
        # distinct lines whose preview checkboxes must not collide.
        adjusted = PlannedLine(
            key=f"f:{food_id}:{dim or '-'}", kind="measured", ingredient_id=line.ingredient_id,
            food_id=food_id, display_text=line.display_text, original_text=line.original_text,
            quantity_text=quantity.format_quantity(quantity.from_canonical(needed, factor)),
            unit_id=line.unit_id, canonical=needed,
        )
        to_buy.append(_trip_line(conn, adjusted, titles))

    for line, _title in raw:
        if line.kind != "check" or line.key in seen_quantityless:
            continue
        seen_quantityless.add(line.key)
        if line.food_id is not None:
            _, gb_nonempty, _ = _pantry_profile(conn, line.food_id, None)
            if gb_nonempty:
                covered.append(line.display_text)
                continue
        to_buy.append(_trip_line(conn, line, check_titles.get(line.key, [])))

    return TripPreview(
        picks=resolved_picks, to_buy=to_buy, covered=covered, to_taste=to_taste
    )


def _trip_line(
    conn: sqlite3.Connection, line: PlannedLine, recipe_titles: list[str]
) -> TripLine:
    unit = units.get_unit(conn, line.unit_id) if line.unit_id else None
    return TripLine(
        key=line.key, kind=line.kind, food_id=line.food_id, display_text=line.display_text,
        original_text=line.original_text, quantity_text=line.quantity_text,
        unit_id=line.unit_id, unit_name=unit.name if unit else None, canonical=line.canonical,
        aisle=_food_category(conn, line.food_id),
        recipe_titles=tuple(recipe_titles),
        purchase_note=_purchase_note(conn, line),
    )


def apply_trip(
    conn: sqlite3.Connection,
    list_id: int,
    picks: list[tuple[int, str | None]],
    *,
    exclude: set[str] | None = None,
    commit: bool = True,
) -> int:
    """Write a previewed trip onto the list. Recomputes server-side (a stale client can't
    resurrect a line the pantry now covers); `exclude` drops preview lines the user unticked."""
    excluded = exclude or set()
    preview = build_trip(conn, picks)
    title_by_id: dict[int, str] = {}
    for recipe_id, _servings in preview.picks:
        detail = recipes.get_recipe(conn, recipe_id)
        if detail is not None:
            title_by_id[recipe_id] = detail.title
    added = 0
    for line in preview.to_buy:
        if line.key in excluded:
            continue
        contributing = [
            rid for rid, title in title_by_id.items() if title in line.recipe_titles
        ] or ([preview.picks[0][0]] if preview.picks else [])
        planned = PlannedLine(
            key=line.key, kind=line.kind, ingredient_id=0, food_id=line.food_id,
            display_text=line.display_text, original_text=line.original_text,
            quantity_text=line.quantity_text, unit_id=line.unit_id, canonical=line.canonical,
        )
        item_id = _write_line(conn, list_id, planned, contributing[0] if contributing else 0)
        # Every contributing recipe shows in provenance, not just the first.
        if item_id is not None:
            for rid in contributing[1:]:
                _record_source(
                    conn, item_id, source_type="recipe", recipe_id=rid,
                    label=line.original_text,
                )
        added += 1
    if commit:
        conn.commit()
    return added


# --------------------------------------------------------------------------------------
# Done shopping -> restock the pantry (the loop-closer)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestockLine:
    item_id: int
    label: str
    pantry_item_id: int | None
    pantry_item_name: str | None
    action_text: str | None  # 'mark full', 'mark have', '+2 kg' - None when nothing to propose
    add_canonical: int | None  # exact items: canonical amount that entered the house
    can_create: bool  # no pantry item yet, but a food to attach one to
    food_id: int | None
    display_text: str


def _purchased_canonical(
    conn: sqlite3.Connection, item: ShoppingItem, pantry_item: pantry.PantryItem
) -> int | None:
    """How much actually entered the house, in canonical units of the pantry item's dimension."""
    item_dim = _dimension(conn, pantry_item.unit_id)
    if item_dim is None:
        return None
    purchase = item.purchase
    if (
        purchase is not None
        and purchase.canonical is not None
        and purchase.unit_dimension == item_dim
    ):
        packages = item.packages or 1
        return purchase.canonical * packages
    if item.canonical_quantity is not None and item.unit_dimension == item_dim:
        return item.canonical_quantity
    return None


def restock_candidates(conn: sqlite3.Connection, list_id: int) -> list[RestockLine]:
    """One proposal per checked line: how to put what you bought back into the pantry."""
    out: list[RestockLine] = []
    for item in list_items(conn, list_id):
        if not item.checked:
            continue
        matched = pantry.items_for_food(conn, item.food_id) if item.food_id else []
        target = matched[0] if matched else None
        action_text: str | None = None
        add_canonical: int | None = None
        if target is not None:
            if target.quantity_mode == "gauge":
                action_text = "mark full"
            elif target.quantity_mode == "binary":
                action_text = "mark have"
            else:
                add_canonical = _purchased_canonical(conn, item, target)
                if add_canonical is not None and target.unit_id is not None:
                    unit = units.get_unit(conn, target.unit_id)
                    if unit and unit.to_canonical_microunits:
                        amount = quantity.from_canonical(
                            add_canonical, unit.to_canonical_microunits
                        )
                        action_text = f"+{quantity.format_quantity(amount)} {unit.name}"
        out.append(
            RestockLine(
                item_id=item.id, label=item.label, pantry_item_id=target.id if target else None,
                pantry_item_name=target.display_name if target else None,
                action_text=action_text, add_canonical=add_canonical,
                can_create=(target is None and item.food_id is not None),
                food_id=item.food_id, display_text=item.display_text,
            )
        )
    return out


def apply_restock(
    conn: sqlite3.Connection,
    list_id: int,
    *,
    restock_item_ids: set[int],
    create_item_ids: set[int],
    create_location_id: int | None,
    create_locations: dict[int, int] | None = None,  # item_id -> per-line location override
    clear_item_ids: set[int] | None = None,
    user_id: int | None = None,
) -> list[str]:
    """Apply the chosen restocks (reason='restock'), create the chosen new pantry items
    (gauge, full - refine later in the pantry), then clear the checked lines.

    A created item lands in its per-line location when one was chosen, else the list-wide
    ``create_location_id`` default.

    ``clear_item_ids`` scopes the clear to the lines the review form actually presented, so an
    item someone else checks off between render and submit is not swept away unseen; None
    (service-level callers) clears every checked line, matching "Clear checked"."""
    summary: list[str] = []
    for line in restock_candidates(conn, list_id):
        if line.item_id in restock_item_ids and line.pantry_item_id is not None:
            target = pantry.get_item(conn, line.pantry_item_id)
            if target is None:
                continue
            if target.quantity_mode == "gauge":
                pantry.set_gauge(
                    conn, target.id, "full", reason="restock", user_id=user_id, commit=False
                )
                summary.append(f"{target.display_name} → full")
            elif target.quantity_mode == "binary":
                pantry.set_have(
                    conn, target.id, True, reason="restock", user_id=user_id, commit=False
                )
                summary.append(f"{target.display_name} → have")
            elif line.add_canonical is not None and target.unit_id is not None:
                unit = units.get_unit(conn, target.unit_id)
                if unit is None or unit.to_canonical_microunits is None:
                    continue
                current = (target.canonical_quantity or 0) + line.add_canonical
                new_text = quantity.plain_str(
                    quantity.from_canonical(current, unit.to_canonical_microunits)
                )
                pantry.set_exact(
                    conn, target.id, new_text, reason="restock", user_id=user_id, commit=False
                )
                summary.append(f"{target.display_name} {line.action_text}")
        elif (
            line.item_id in create_item_ids
            and line.can_create
            and (loc_id := (create_locations or {}).get(line.item_id) or create_location_id)
            is not None
        ):
            new_id = pantry.add_item(
                conn,
                pantry.PantryItemInput(
                    display_name=line.display_text, location_id=loc_id,
                    quantity_mode="gauge", gauge="full",
                ),
                user_id=user_id, commit=False,
            )
            if new_id:
                summary.append(f"{line.display_text} → tracked (full)")
    if clear_item_ids is None:
        conn.execute(
            "DELETE FROM shopping_list_items WHERE list_id = ? AND checked = 1", (list_id,)
        )
    else:
        for item_id in clear_item_ids:
            conn.execute(
                "DELETE FROM shopping_list_items WHERE list_id = ? AND checked = 1 AND id = ?",
                (list_id, item_id),
            )
    conn.commit()
    return summary


# --------------------------------------------------------------------------------------
# Mutations + export
# --------------------------------------------------------------------------------------


def set_purchase_info(
    conn: sqlite3.Connection,
    food_id: int,
    *,
    quantity_text: str | None,
    unit: str | None,
    label: str | None,
    commit: bool = True,
) -> None:
    """The list page's inline "how do you buy this?" answer (delegates to foods)."""
    try:
        foods.set_purchase(
            conn, food_id, quantity_text=quantity_text, unit=unit, label=label, commit=commit
        )
    except (foods.FoodError, quantity.QuantityError) as exc:
        raise ShoppingError(str(exc)) from exc


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


def clear_recipe_lines(conn: sqlite3.Connection, list_id: int, *, commit: bool = True) -> int:
    """Remove unchecked, non-manual lines that came only from recipe/meal-plan sources.

    Lets a plan re-sync REPLACE its own contribution instead of doubling it (review finding).
    Staples, hand-added lines, and anything already checked off are left untouched.
    """
    cur = conn.execute(
        """DELETE FROM shopping_list_items
           WHERE list_id = ? AND checked = 0 AND is_manual = 0
             AND id IN (
                 SELECT sli.id FROM shopping_list_items sli
                 WHERE sli.list_id = ?
                   AND EXISTS (SELECT 1 FROM shopping_item_sources s
                               WHERE s.item_id = sli.id
                                 AND s.source_type IN ('recipe','meal_plan'))
                   AND NOT EXISTS (SELECT 1 FROM shopping_item_sources s
                                   WHERE s.item_id = sli.id
                                     AND s.source_type IN ('staple','manual'))
             )""",
        (list_id, list_id),
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


def to_reminders_text(conn: sqlite3.Connection, list_id: int) -> str:
    """Unchecked items, one per line, no aisle headers - paste straight into Apple Reminders (each
    line becomes its own reminder). This is the household's away-from-home path: the list rides
    iCloud, so no server exposure is needed."""
    return "\n".join(item.label for item in list_items(conn, list_id) if not item.checked)


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
