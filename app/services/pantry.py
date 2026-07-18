"""Pantry (Phase 4): approximate household inventory with graduated granularity.

Three quantity modes (docs/06 section 1): ``exact`` counts for discrete items (cans, jars), a
``gauge`` (full/half/low/out) for bulk staples (flour, rice, oil), and ``binary`` have/out for
condiments. Forcing exact quantities on everything is the documented #1 cause of pantry abandonment,
so exact is opt-in per item.

Every mutation appends a ``pantry_adjustments`` row (history + one-tap cook Undo) and refreshes the
``pantry_items`` row in place - the item row is the *current state*, the adjustments are the audit
log (a lightweight log, NOT Grocy's mandatory double-entry ledger). All exact math routes through
``app.services.quantity`` (canonical mg/uL/milli-each), never binary float.

Mutation functions take ``commit`` so the cook-deduction path (Phase 4c) can run pantry writes
inside ``record_cook``'s single transaction; routes call them with the default ``commit=True``.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.security import now_iso
from app.services import quantity, units

_GAUGE_CYCLE: tuple[str, ...] = ("full", "half", "low", "out")
_QUANTITY_MODES: frozenset[str] = frozenset({"exact", "gauge", "binary"})
_REMOVE_REASONS: frozenset[str] = frozenset({"manual_remove", "spoiled"})


class PantryError(ValueError):
    """Invalid pantry input (bad amount, unknown location/item, or a mode mismatch)."""


# --------------------------------------------------------------------------------------
# Locations (created inline from wherever you need them - Grocy's master-data detours are
# its #1 UI complaint, docs/06 section 1)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Location:
    id: int
    name: str
    is_freezer: bool
    sort_order: int


def create_location(
    conn: sqlite3.Connection, name: str, *, is_freezer: bool = False, commit: bool = True
) -> int:
    """Create a location (or return the existing one with that name). Returns its id."""
    clean = name.strip()
    if not clean:
        raise PantryError("a location needs a name")
    existing = conn.execute("SELECT id FROM locations WHERE name = ?", (clean,)).fetchone()
    if existing is not None:
        return int(existing["id"])
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM locations").fetchone()
    cur = conn.execute(
        "INSERT INTO locations (name, is_freezer, sort_order) VALUES (?, ?, ?)",
        (clean, 1 if is_freezer else 0, int(nxt["n"])),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def list_locations(conn: sqlite3.Connection) -> list[Location]:
    rows = conn.execute("SELECT * FROM locations ORDER BY sort_order, name").fetchall()
    return [
        Location(
            id=int(r["id"]), name=r["name"], is_freezer=bool(r["is_freezer"]),
            sort_order=int(r["sort_order"]),
        )
        for r in rows
    ]


# --------------------------------------------------------------------------------------
# Pantry items
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PantryItemInput:
    display_name: str
    location_id: int
    quantity_mode: str = "gauge"
    food: str | None = None            # autocomplete text; resolved/created to a food_id
    quantity_text: str | None = None   # exact mode
    unit: str | None = None            # exact mode unit (free text -> unit_id)
    gauge: str | None = None           # gauge mode initial (defaults to 'full')
    have: bool | None = None           # binary mode initial (defaults to True)
    is_staple: bool = False
    min_quantity_text: str | None = None
    expires_on: str | None = None
    step_down_on_cook: bool = False


@dataclass(frozen=True, slots=True)
class PantryItem:
    id: int
    food_id: int | None
    display_name: str
    location_id: int
    location_name: str
    quantity_mode: str
    quantity_text: str | None
    unit_id: int | None
    unit_name: str | None
    canonical_quantity: int | None
    gauge: str | None
    have: int | None
    is_staple: bool
    min_quantity_text: str | None
    canonical_min_quantity: int | None
    expires_on: str | None
    step_down_on_cook: bool
    updated_at: str
    needs_restock: bool
    display_quantity: str


def _resolve_or_create_food(conn: sqlite3.Connection, name: str | None) -> int | None:
    """Resolve a food by alias/name (case-insensitive), creating a confirmed food if new.

    Mirrors recipes._resolve_food_id so a pantry item and a recipe ingredient for the same food
    share one food_id - that shared id is what pantry-aware matching (Phase 4e) joins on.
    """
    key = (name or "").strip()
    if not key:
        return None
    row = conn.execute("SELECT food_id FROM food_aliases WHERE alias = ?", (key,)).fetchone()
    if row is not None:
        return int(row["food_id"])
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (key,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO foods (name, status) VALUES (?, 'confirmed')", (key,))
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def _canonical(
    conn: sqlite3.Connection, quantity_text: str | None, unit_id: int | None
) -> int | None:
    """Canonical micro-unit value for an exact amount, or None when it can't be resolved exactly."""
    if quantity_text is None or unit_id is None:
        return None
    unit = units.get_unit(conn, unit_id)
    if unit is None or unit.to_canonical_microunits is None:
        return None
    value = quantity.parse_quantity(quantity_text)
    return quantity.to_canonical(value, unit.to_canonical_microunits)


def _current_value(item: sqlite3.Row) -> Decimal:
    """The exact item's current amount as a Decimal (zero when unset)."""
    return quantity.parse_quantity(item["quantity_text"]) if item["quantity_text"] else Decimal(0)


def _signed_decimal(text: str) -> Decimal:
    """Parse a signed amount ('-1', '0.5', '-1/2') for stepper deltas (parse_quantity bans '-')."""
    raw = " ".join(text.split())
    negative = raw.startswith("-")
    magnitude = quantity.parse_quantity(raw[1:] if negative else raw)
    return -magnitude if negative else magnitude


def add_item(
    conn: sqlite3.Connection,
    data: PantryItemInput,
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> int:
    """Create a pantry item and record its opening state as a 'correction' adjustment."""
    if data.quantity_mode not in _QUANTITY_MODES:
        raise PantryError(f"unknown quantity mode: {data.quantity_mode!r}")
    name = data.display_name.strip()
    if not name:
        raise PantryError("a pantry item needs a name")
    if conn.execute("SELECT 1 FROM locations WHERE id = ?", (data.location_id,)).fetchone() is None:
        raise PantryError("unknown location")

    food_id = _resolve_or_create_food(conn, data.food or name)
    unit = units.resolve_unit(conn, data.unit) if (data.unit and data.unit.strip()) else None
    unit_id = unit.id if unit else None

    quantity_text: str | None = None
    gauge: str | None = None
    have: int | None = None
    if data.quantity_mode == "exact":
        if data.quantity_text is None or not data.quantity_text.strip():
            raise PantryError("an exact item needs a starting quantity")
        quantity.parse_quantity(data.quantity_text)  # validate
        quantity_text = data.quantity_text.strip()
    elif data.quantity_mode == "gauge":
        gauge = data.gauge if data.gauge in _GAUGE_CYCLE else "full"
    else:  # binary
        have = 1 if (data.have is None or data.have) else 0

    canonical_quantity = _canonical(conn, quantity_text, unit_id)
    min_text = data.min_quantity_text.strip() if data.min_quantity_text else None
    if min_text:
        quantity.parse_quantity(min_text)  # validate
    canonical_min = _canonical(conn, min_text, unit_id)
    stamp = now_iso()

    cur = conn.execute(
        """INSERT INTO pantry_items
           (food_id, display_name, location_id, quantity_mode, quantity_text, unit_id,
            canonical_quantity, gauge, have, is_staple, min_quantity_text, canonical_min_quantity,
            expires_on, step_down_on_cook, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            food_id, name, data.location_id, data.quantity_mode, quantity_text, unit_id,
            canonical_quantity, gauge, have, 1 if data.is_staple else 0, min_text, canonical_min,
            data.expires_on or None, 1 if data.step_down_on_cook else 0, stamp, user_id,
        ),
    )
    item_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    _record_adjustment(
        conn, item_id, food_id, reason="correction", user_id=user_id, source="add",
        delta_quantity_text=quantity_text, canonical_delta=canonical_quantity,
        to_gauge=gauge, to_have=have,
    )
    if commit:
        conn.commit()
    return item_id


# --------------------------------------------------------------------------------------
# Adjustments (the append-only history + current-state update primitive)
# --------------------------------------------------------------------------------------


def _record_adjustment(
    conn: sqlite3.Connection,
    item_id: int | None,
    food_id: int | None,
    *,
    reason: str,
    user_id: int | None,
    source: str | None = None,
    delta_quantity_text: str | None = None,
    canonical_delta: int | None = None,
    from_gauge: str | None = None,
    to_gauge: str | None = None,
    from_have: int | None = None,
    to_have: int | None = None,
    cook_log_id: int | None = None,
    batch_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO pantry_adjustments
           (pantry_item_id, food_id, delta_quantity_text, canonical_delta, from_gauge, to_gauge,
            from_have, to_have, reason, source, cook_log_id, batch_id, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id, food_id, delta_quantity_text, canonical_delta, from_gauge, to_gauge,
            from_have, to_have, reason, source, cook_log_id, batch_id, user_id,
        ),
    )


def _row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM pantry_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise PantryError("unknown pantry item")
    return row


def set_exact(
    conn: sqlite3.Connection,
    item_id: int,
    quantity_text: str,
    *,
    reason: str = "correction",
    user_id: int | None = None,
    cook_log_id: int | None = None,
    batch_id: str | None = None,
    commit: bool = True,
) -> None:
    """Set an exact item's quantity, recording the signed delta (canonical + text)."""
    item = _row(conn, item_id)
    if item["quantity_mode"] != "exact":
        raise PantryError("not an exact-quantity item")
    new_value = quantity.parse_quantity(quantity_text)
    delta = new_value - _current_value(item)
    new_canonical = _canonical(conn, quantity_text, item["unit_id"])
    old_canonical = item["canonical_quantity"]
    canonical_delta = (
        new_canonical - old_canonical
        if (new_canonical is not None and old_canonical is not None)
        else new_canonical
    )
    stamp = now_iso()
    conn.execute(
        """UPDATE pantry_items
           SET quantity_text = ?, canonical_quantity = ?, updated_at = ?, updated_by = ?
           WHERE id = ?""",
        (quantity.plain_str(new_value), new_canonical, stamp, user_id, item_id),
    )
    _record_adjustment(
        conn, item_id, item["food_id"], reason=reason, user_id=user_id,
        delta_quantity_text=quantity.format_quantity(delta), canonical_delta=canonical_delta,
        cook_log_id=cook_log_id, batch_id=batch_id,
    )
    if commit:
        conn.commit()


def step_exact(
    conn: sqlite3.Connection,
    item_id: int,
    delta_text: str,
    *,
    reason: str = "correction",
    user_id: int | None = None,
    commit: bool = True,
) -> None:
    """Nudge an exact item by a signed amount (+/- steppers), clamped at zero."""
    item = _row(conn, item_id)
    if item["quantity_mode"] != "exact":
        raise PantryError("not an exact-quantity item")
    try:
        delta = _signed_decimal(delta_text)
    except (quantity.QuantityError, InvalidOperation) as exc:
        raise PantryError(f"bad step amount: {delta_text!r}") from exc
    new_value = _current_value(item) + delta
    if new_value < 0:
        new_value = Decimal(0)
    set_exact(
        conn, item_id, quantity.plain_str(new_value), reason=reason, user_id=user_id,
        commit=commit,
    )


def set_gauge(
    conn: sqlite3.Connection,
    item_id: int,
    gauge: str,
    *,
    reason: str = "stock_take",
    user_id: int | None = None,
    cook_log_id: int | None = None,
    batch_id: str | None = None,
    commit: bool = True,
) -> None:
    """Set a gauge item to an explicit level (stock-take, restock, cook step-down)."""
    if gauge not in _GAUGE_CYCLE:
        raise PantryError(f"unknown gauge: {gauge!r}")
    item = _row(conn, item_id)
    if item["quantity_mode"] != "gauge":
        raise PantryError("not a gauge item")
    _apply_gauge(
        conn, item, gauge, reason=reason, user_id=user_id, cook_log_id=cook_log_id,
        batch_id=batch_id,
    )
    if commit:
        conn.commit()


def cycle_gauge(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    reason: str = "correction",
    user_id: int | None = None,
    commit: bool = True,
) -> str:
    """Advance a gauge one step (full->half->low->out->full). Returns the new level."""
    item = _row(conn, item_id)
    if item["quantity_mode"] != "gauge":
        raise PantryError("not a gauge item")
    current = item["gauge"] if item["gauge"] in _GAUGE_CYCLE else "full"
    new_gauge = _GAUGE_CYCLE[(_GAUGE_CYCLE.index(current) + 1) % len(_GAUGE_CYCLE)]
    _apply_gauge(conn, item, new_gauge, reason=reason, user_id=user_id)
    if commit:
        conn.commit()
    return new_gauge


def _apply_gauge(
    conn: sqlite3.Connection,
    item: sqlite3.Row,
    new_gauge: str,
    *,
    reason: str,
    user_id: int | None,
    cook_log_id: int | None = None,
    batch_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE pantry_items SET gauge = ?, updated_at = ?, updated_by = ? WHERE id = ?",
        (new_gauge, now_iso(), user_id, item["id"]),
    )
    _record_adjustment(
        conn, int(item["id"]), item["food_id"], reason=reason, user_id=user_id,
        from_gauge=item["gauge"], to_gauge=new_gauge, cook_log_id=cook_log_id, batch_id=batch_id,
    )


def set_have(
    conn: sqlite3.Connection,
    item_id: int,
    have: bool,
    *,
    reason: str = "stock_take",
    user_id: int | None = None,
    cook_log_id: int | None = None,
    batch_id: str | None = None,
    commit: bool = True,
) -> None:
    """Set a binary item's have/out state."""
    item = _row(conn, item_id)
    if item["quantity_mode"] != "binary":
        raise PantryError("not a have/out item")
    new_have = 1 if have else 0
    conn.execute(
        "UPDATE pantry_items SET have = ?, updated_at = ?, updated_by = ? WHERE id = ?",
        (new_have, now_iso(), user_id, item_id),
    )
    _record_adjustment(
        conn, item_id, item["food_id"], reason=reason, user_id=user_id,
        from_have=item["have"], to_have=new_have, cook_log_id=cook_log_id, batch_id=batch_id,
    )
    if commit:
        conn.commit()


def toggle_have(
    conn: sqlite3.Connection, item_id: int, *, user_id: int | None = None, commit: bool = True
) -> bool:
    """Flip a binary item and return its new have state."""
    item = _row(conn, item_id)
    if item["quantity_mode"] != "binary":
        raise PantryError("not a have/out item")
    new_have = 0 if item["have"] else 1
    set_have(conn, item_id, bool(new_have), reason="correction", user_id=user_id, commit=commit)
    return bool(new_have)


def set_staple(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    is_staple: bool,
    min_quantity_text: str | None = None,
    user_id: int | None = None,
    commit: bool = True,
) -> None:
    """Set the staple flag and optional restock threshold. Metadata only - no adjustment row."""
    item = _row(conn, item_id)
    min_text = min_quantity_text.strip() if min_quantity_text else None
    if min_text:
        quantity.parse_quantity(min_text)  # validate
    canonical_min = _canonical(conn, min_text, item["unit_id"])
    conn.execute(
        """UPDATE pantry_items
           SET is_staple = ?, min_quantity_text = ?, canonical_min_quantity = ?,
               updated_at = ?, updated_by = ?
           WHERE id = ?""",
        (1 if is_staple else 0, min_text, canonical_min, now_iso(), user_id, item_id),
    )
    if commit:
        conn.commit()


def remove_item(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    reason: str = "manual_remove",
    user_id: int | None = None,
    delete: bool = False,
    commit: bool = True,
) -> None:
    """Empty an item (exact->0, gauge->out, binary->out) or delete the row, writing history.

    ``delete`` hard-removes the row; the adjustment survives (pantry_item_id ON DELETE SET NULL,
    food_id retained) so "you tossed spinach twice this month" still works.
    """
    if reason not in _REMOVE_REASONS:
        raise PantryError(f"remove reason must be one of {sorted(_REMOVE_REASONS)}")
    item = _row(conn, item_id)
    mode = item["quantity_mode"]
    stamp = now_iso()
    if mode == "exact":
        old_canonical = item["canonical_quantity"]
        old_value = _current_value(item)
        _record_adjustment(
            conn, item_id, item["food_id"], reason=reason, user_id=user_id,
            delta_quantity_text=quantity.format_quantity(-old_value),
            canonical_delta=(-old_canonical if old_canonical is not None else None),
        )
        conn.execute(
            "UPDATE pantry_items SET quantity_text = '0', canonical_quantity = 0, "
            "updated_at = ?, updated_by = ? WHERE id = ?",
            (stamp, user_id, item_id),
        )
    elif mode == "gauge":
        _record_adjustment(
            conn, item_id, item["food_id"], reason=reason, user_id=user_id,
            from_gauge=item["gauge"], to_gauge="out",
        )
        conn.execute(
            "UPDATE pantry_items SET gauge = 'out', updated_at = ?, updated_by = ? WHERE id = ?",
            (stamp, user_id, item_id),
        )
    else:  # binary
        _record_adjustment(
            conn, item_id, item["food_id"], reason=reason, user_id=user_id,
            from_have=item["have"], to_have=0,
        )
        conn.execute(
            "UPDATE pantry_items SET have = 0, updated_at = ?, updated_by = ? WHERE id = ?",
            (stamp, user_id, item_id),
        )
    if delete:
        conn.execute("DELETE FROM pantry_items WHERE id = ?", (item_id,))
    if commit:
        conn.commit()


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------


def _needs_restock(
    mode: str,
    is_staple: bool,
    gauge: str | None,
    have: int | None,
    canonical_quantity: int | None,
    canonical_min: int | None,
) -> bool:
    """A staple that is out or below its threshold is a shopping candidate (docs/03 line 353)."""
    if not is_staple:
        return False
    if mode == "gauge":
        return gauge == "out"
    if mode == "binary":
        return have == 0
    if canonical_quantity is None:
        return False
    if canonical_min is not None:
        return canonical_quantity < canonical_min
    return canonical_quantity <= 0


def _display_quantity(row: sqlite3.Row) -> str:
    mode = row["quantity_mode"]
    if mode == "gauge":
        return row["gauge"] or "full"
    if mode == "binary":
        return "have" if row["have"] else "out"
    qty = row["quantity_text"] or "0"
    unit_name = row["unit_name"]
    return f"{qty} {unit_name}" if unit_name else qty


def _to_item(row: sqlite3.Row) -> PantryItem:
    return PantryItem(
        id=int(row["id"]),
        food_id=row["food_id"],
        display_name=row["display_name"],
        location_id=int(row["location_id"]),
        location_name=row["location_name"],
        quantity_mode=row["quantity_mode"],
        quantity_text=row["quantity_text"],
        unit_id=row["unit_id"],
        unit_name=row["unit_name"],
        canonical_quantity=row["canonical_quantity"],
        gauge=row["gauge"],
        have=row["have"],
        is_staple=bool(row["is_staple"]),
        min_quantity_text=row["min_quantity_text"],
        canonical_min_quantity=row["canonical_min_quantity"],
        expires_on=row["expires_on"],
        step_down_on_cook=bool(row["step_down_on_cook"]),
        updated_at=row["updated_at"],
        needs_restock=_needs_restock(
            row["quantity_mode"], bool(row["is_staple"]), row["gauge"], row["have"],
            row["canonical_quantity"], row["canonical_min_quantity"],
        ),
        display_quantity=_display_quantity(row),
    )


_ITEM_SELECT = """
    SELECT pi.*, l.name AS location_name, u.name AS unit_name
    FROM pantry_items pi
    JOIN locations l ON l.id = pi.location_id
    LEFT JOIN units u ON u.id = pi.unit_id
"""


def list_items(conn: sqlite3.Connection, *, location_id: int | None = None) -> list[PantryItem]:
    if location_id is not None:
        rows = conn.execute(
            _ITEM_SELECT + " WHERE pi.location_id = ? ORDER BY pi.display_name COLLATE NOCASE",
            (location_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            _ITEM_SELECT + " ORDER BY l.sort_order, pi.display_name COLLATE NOCASE"
        ).fetchall()
    return [_to_item(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> PantryItem | None:
    row = conn.execute(_ITEM_SELECT + " WHERE pi.id = ?", (item_id,)).fetchone()
    return _to_item(row) if row else None


def shopping_candidates(conn: sqlite3.Connection) -> list[PantryItem]:
    """Staples that are out or below threshold - the pantry's contribution to the shopping list."""
    return [item for item in list_items(conn) if item.needs_restock]


def items_for_food(conn: sqlite3.Connection, food_id: int | None) -> list[PantryItem]:
    """Every pantry item mapped to a food - the join a recipe ingredient uses to find its item."""
    if food_id is None:
        return []
    rows = conn.execute(
        _ITEM_SELECT + " WHERE pi.food_id = ? ORDER BY pi.id", (food_id,)
    ).fetchall()
    return [_to_item(r) for r in rows]


def deduct_canonical(
    conn: sqlite3.Connection,
    item_id: int,
    used_canonical: int,
    *,
    reason: str = "cook",
    cook_log_id: int | None = None,
    batch_id: str | None = None,
    user_id: int | None = None,
    commit: bool = False,
) -> int:
    """Subtract a canonical amount from an exact item (clamped at zero); returns the applied delta.

    Works in canonical micro-units so a recipe measured in one unit can deduct from a pantry item
    measured in another of the same dimension. The caller must have verified matching dimensions.
    Defaults to commit=False so a cook applies every line in one batch before committing.
    """
    item = _row(conn, item_id)
    if item["quantity_mode"] != "exact":
        raise PantryError("not an exact-quantity item")
    unit = units.get_unit(conn, item["unit_id"]) if item["unit_id"] else None
    if unit is None or unit.to_canonical_microunits is None:
        raise PantryError("item has no exact unit to deduct from")
    factor = unit.to_canonical_microunits
    old_canonical = item["canonical_quantity"] or 0
    new_canonical = max(0, old_canonical - used_canonical)
    applied_delta = new_canonical - old_canonical  # <= 0
    old_value = quantity.from_canonical(old_canonical, factor)
    new_value = quantity.from_canonical(new_canonical, factor)
    conn.execute(
        """UPDATE pantry_items
           SET quantity_text = ?, canonical_quantity = ?, updated_at = ?, updated_by = ?
           WHERE id = ?""",
        (quantity.plain_str(new_value), new_canonical, now_iso(), user_id, item_id),
    )
    _record_adjustment(
        conn, item_id, item["food_id"], reason=reason, user_id=user_id,
        delta_quantity_text=quantity.format_quantity(new_value - old_value),
        canonical_delta=applied_delta, cook_log_id=cook_log_id, batch_id=batch_id,
    )
    if commit:
        conn.commit()
    return applied_delta


def new_batch_id() -> str:
    """Opaque id grouping one cook's deductions for one-tap Undo (Phase 4c)."""
    return uuid.uuid4().hex
