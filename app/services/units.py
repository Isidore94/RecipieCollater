"""Units: the canonical reference set, idempotent seeding, and name/alias resolution.

Unit factors are integer canonical micro-units (mg / uL / milli-each) so quantity math
stays exact (CONVENTIONS 1; see app.services.quantity). Imperial factors are rounded to
the nearest micro-unit - a sub-milligram error no recipe cares about. Approximate units
('pinch', 'to taste') carry no factor.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# (name, plural_name, abbreviation, dimension, to_canonical_microunits, aliases)
_CoreUnit = tuple[str, str | None, str | None, str, int | None, tuple[str, ...]]

_CORE_UNITS: tuple[_CoreUnit, ...] = (
    # Mass -> milligrams
    ("gram", "grams", "g", "mass", 1_000, ("gram", "grams", "gramme", "grammes", "g", "gr")),
    ("kilogram", "kilograms", "kg", "mass", 1_000_000,
     ("kilogram", "kilograms", "kilogramme", "kilo", "kilos", "kg")),
    ("milligram", "milligrams", "mg", "mass", 1, ("milligram", "milligrams", "mg")),
    ("ounce", "ounces", "oz", "mass", 28_350, ("ounce", "ounces", "oz")),
    ("pound", "pounds", "lb", "mass", 453_592, ("pound", "pounds", "lb", "lbs")),
    # Volume -> microlitres
    ("millilitre", "millilitres", "ml", "volume", 1_000,
     ("millilitre", "milliliter", "millilitres", "milliliters", "ml", "cc")),
    ("litre", "litres", "l", "volume", 1_000_000, ("litre", "liter", "litres", "liters", "l")),
    ("teaspoon", "teaspoons", "tsp", "volume", 4_929, ("teaspoon", "teaspoons", "tsp", "tsps")),
    ("tablespoon", "tablespoons", "tbsp", "volume", 14_787,
     ("tablespoon", "tablespoons", "tbsp", "tbsps", "tbs")),
    ("fluid ounce", "fluid ounces", "fl oz", "volume", 29_574,
     ("fluid ounce", "fluid ounces", "fl oz", "floz")),
    ("cup", "cups", "c", "volume", 236_588, ("cup", "cups", "c")),
    ("pint", "pints", "pt", "volume", 473_176, ("pint", "pints", "pt")),
    ("quart", "quarts", "qt", "volume", 946_353, ("quart", "quarts", "qt")),
    ("gallon", "gallons", "gal", "volume", 3_785_412, ("gallon", "gallons", "gal")),
    # Count -> milli-each. 'each' is the canonical bare-count unit.
    ("each", "each", None, "count", 1_000,
     ("each", "ea", "piece", "pieces", "whole", "unit", "units")),
    ("dozen", "dozen", None, "count", 12_000, ("dozen", "dozens", "doz")),
    # Approximate: no factor.
    ("pinch", "pinches", None, "approx", None, ("pinch", "pinches")),
    ("dash", "dashes", None, "approx", None, ("dash", "dashes")),
    ("to taste", None, None, "approx", None, ("to taste",)),
    ("some", None, None, "approx", None, ("some",)),
    ("handful", "handfuls", None, "approx", None, ("handful", "handfuls")),
)


@dataclass(frozen=True, slots=True)
class Unit:
    id: int
    name: str
    plural_name: str | None
    abbreviation: str | None
    dimension: str
    to_canonical_microunits: int | None


def _row_to_unit(row: sqlite3.Row) -> Unit:
    return Unit(
        id=row["id"],
        name=row["name"],
        plural_name=row["plural_name"],
        abbreviation=row["abbreviation"],
        dimension=row["dimension"],
        to_canonical_microunits=row["to_canonical_microunits"],
    )


def seed_core_units(conn: sqlite3.Connection) -> int:
    """Insert the core kitchen units and their aliases. Idempotent; returns units added."""
    added = 0
    for name, plural, abbreviation, dimension, factor, aliases in _CORE_UNITS:
        cur = conn.execute(
            """INSERT OR IGNORE INTO units
               (name, plural_name, abbreviation, dimension, to_canonical_microunits)
               VALUES (?, ?, ?, ?, ?)""",
            (name, plural, abbreviation, dimension, factor),
        )
        if cur.rowcount:
            added += 1
        row = conn.execute("SELECT id FROM units WHERE name = ?", (name,)).fetchone()
        unit_id = int(row["id"])
        for alias in aliases:
            conn.execute(
                "INSERT OR IGNORE INTO unit_aliases (alias, unit_id) VALUES (?, ?)",
                (alias, unit_id),
            )
    conn.commit()
    return added


def get_unit(conn: sqlite3.Connection, unit_id: int) -> Unit | None:
    row = conn.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
    return _row_to_unit(row) if row else None


def list_units(conn: sqlite3.Connection) -> list[Unit]:
    rows = conn.execute(
        "SELECT * FROM units ORDER BY dimension, to_canonical_microunits"
    ).fetchall()
    return [_row_to_unit(r) for r in rows]


def resolve_unit(conn: sqlite3.Connection, text: str) -> Unit | None:
    """Resolve a free-text unit ('Tbsp', 'cups', 'g') to its canonical Unit, or None."""
    key = text.strip()
    if not key:
        return None
    row = conn.execute("SELECT unit_id FROM unit_aliases WHERE alias = ?", (key,)).fetchone()
    if row is not None:
        return get_unit(conn, int(row["unit_id"]))
    row = conn.execute("SELECT id FROM units WHERE name = ? COLLATE NOCASE", (key,)).fetchone()
    return get_unit(conn, int(row["id"])) if row else None
