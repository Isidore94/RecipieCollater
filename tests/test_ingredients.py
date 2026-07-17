"""Ingredient-line normalization: quantity split, food/note split (pure), and unit resolution."""

from __future__ import annotations

import sqlite3

import pytest

from app.extraction import ExtractedIngredient
from app.services import ingredients
from app.services.units import seed_core_units


@pytest.mark.parametrize(
    ("line", "quantity", "remainder"),
    [
        ("2 cups flour", "2", "cups flour"),
        ("1 1/2 cups flour", "1 1/2", "cups flour"),
        ("1/2 onion", "1/2", "onion"),
        ("2.5 oz walnuts", "2.5", "oz walnuts"),
        ("½ cup sugar", "1/2", "cup sugar"),  # unicode 1/2
        ("1½ cups milk", "1 1/2", "cups milk"),  # "1 1/2"
        ("Salt to taste", None, "Salt to taste"),
    ],
)
def test_parse_quantity_prefix(line: str, quantity: str | None, remainder: str) -> None:
    assert ingredients.parse_quantity_prefix(line) == (quantity, remainder)


@pytest.mark.parametrize(
    ("text", "food", "note"),
    [
        ("olive oil, finely chopped", "olive oil", "finely chopped"),
        ("flour (sifted)", "flour", "sifted"),
        ("sugar", "sugar", None),
        ("carrots, peeled and chopped", "carrots", "peeled and chopped"),
    ],
)
def test_split_food_note(text: str, food: str, note: str | None) -> None:
    assert ingredients.split_food_note(text) == (food, note)


def _seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_core_units(conn)
    return conn


def test_normalize_structures_measured_ingredient(migrated_db: sqlite3.Connection) -> None:
    item = ExtractedIngredient(original_text="2 tablespoons olive oil, finely chopped")
    result = ingredients.normalize_ingredient(_seeded(migrated_db), item)
    assert result.quantity_text == "2"
    assert result.unit == "tablespoons"  # resolves to the 'tablespoon' unit
    assert result.food == "olive oil"
    assert result.note == "finely chopped"


def test_normalize_handles_unicode_and_resolves_unit(migrated_db: sqlite3.Connection) -> None:
    result = ingredients.normalize_ingredient(
        _seeded(migrated_db), ExtractedIngredient(original_text="½ cup sugar")
    )
    assert result.quantity_text == "1/2"
    assert result.unit == "cup"
    assert result.food == "sugar"


def test_normalize_leaves_unmeasured_lines_raw(migrated_db: sqlite3.Connection) -> None:
    conn = _seeded(migrated_db)
    # No amount at all.
    salt = ingredients.normalize_ingredient(
        conn, ExtractedIngredient(original_text="Salt to taste")
    )
    assert salt.quantity_text is None
    assert salt.original_text == "Salt to taste"
    # An amount but no ontology unit ("clove" is not a unit) stays raw, never a bare amount.
    clove = ingredients.normalize_ingredient(
        conn, ExtractedIngredient(original_text="1 clove garlic, minced")
    )
    assert clove.quantity_text is None
    assert clove.unit is None
