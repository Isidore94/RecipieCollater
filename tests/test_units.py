"""Unit ontology: seeding idempotency, resolution, and canonical factors (CONVENTIONS 1)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from app.services import quantity, units


def test_seed_is_idempotent(migrated_db: sqlite3.Connection) -> None:
    first = units.seed_core_units(migrated_db)
    second = units.seed_core_units(migrated_db)
    assert first > 0
    assert second == 0  # nothing new to add on the second run
    assert len(units.list_units(migrated_db)) == first


def test_resolve_by_name_plural_alias_and_case(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    for text in ("cup", "Cups", "c", "  CUP  "):
        resolved = units.resolve_unit(migrated_db, text)
        assert resolved is not None
        assert resolved.name == "cup"
    tbsp = units.resolve_unit(migrated_db, "Tablespoons")
    assert tbsp is not None
    assert tbsp.abbreviation == "tbsp"
    assert units.resolve_unit(migrated_db, "nonsense-unit") is None
    assert units.resolve_unit(migrated_db, "  ") is None


def test_cup_factor_and_cross_unit_conversion(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    cup = units.resolve_unit(migrated_db, "cup")
    tbsp = units.resolve_unit(migrated_db, "tbsp")
    assert cup is not None and tbsp is not None
    assert cup.dimension == "volume"
    assert cup.to_canonical_microunits == 236_588
    factor_cup = cup.to_canonical_microunits
    factor_tbsp = tbsp.to_canonical_microunits
    assert factor_cup is not None and factor_tbsp is not None
    # 1 cup == 16 tablespoons (within canonical rounding).
    converted = quantity.convert(Decimal("1"), factor_cup, factor_tbsp)
    assert converted.to_integral_value() == Decimal("16")


def test_metric_factors_are_exact(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    kilogram = units.resolve_unit(migrated_db, "kg")
    litre = units.resolve_unit(migrated_db, "l")
    assert kilogram is not None and kilogram.to_canonical_microunits == 1_000_000
    assert litre is not None and litre.to_canonical_microunits == 1_000_000


def test_each_is_the_bare_count_unit(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    each = units.resolve_unit(migrated_db, "each")
    assert each is not None
    assert each.dimension == "count"
    assert each.to_canonical_microunits == 1_000
    pieces = units.resolve_unit(migrated_db, "pieces")
    assert pieces is not None and pieces.name == "each"


def test_approximate_units_have_no_factor(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    for text in ("pinch", "to taste", "handful"):
        unit = units.resolve_unit(migrated_db, text)
        assert unit is not None
        assert unit.dimension == "approx"
        assert unit.to_canonical_microunits is None
