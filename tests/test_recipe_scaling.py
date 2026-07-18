"""Ephemeral serving scaler: exact scaling, kitchen fractions, per-mode behaviour."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.services import recipes, units


def _detail(migrated_db: sqlite3.Connection) -> recipes.RecipeDetail:
    units.seed_core_units(migrated_db)
    recipe_id = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Pasta",
            base_servings="4",
            ingredients=[
                recipes.IngredientInput(quantity_text="2", unit="cups", food="flour"),
                recipes.IngredientInput(quantity_text="1", unit="tbsp", food="olive oil"),
                recipes.IngredientInput(
                    quantity_text="1", unit="tsp", food="salt", scaling_mode="fixed"
                ),
                recipes.IngredientInput(
                    original_text="black pepper, to taste", food="pepper", scaling_mode="to_taste"
                ),
                recipes.IngredientInput(original_text="a handful of basil"),
            ],
        ),
    )
    detail = recipes.get_recipe(migrated_db, recipe_id)
    assert detail is not None
    return detail


def test_scale_up_4_to_6(migrated_db: sqlite3.Connection) -> None:
    scaled = recipes.scale_ingredients(_detail(migrated_db), "6")
    assert scaled[0].display == "3 cups flour"  # 2 -> 3, canonical plural unit
    assert scaled[1].display == "1 1/2 tablespoons olive oil"  # 1 -> 1 1/2
    assert scaled[2].display == "1 tsp salt"  # fixed: original wording kept
    assert scaled[3].display == "black pepper, to taste"  # to_taste: original wording kept
    assert scaled[4].display == "a handful of basil"  # no amount: original text


def test_scale_down_4_to_2(migrated_db: sqlite3.Connection) -> None:
    scaled = recipes.scale_ingredients(_detail(migrated_db), "2")
    assert scaled[0].display == "1 cup flour"  # 2 -> 1, singular
    assert scaled[1].display == "1/2 tablespoon olive oil"  # 1 -> 1/2, singular


def test_unscaled_keeps_original_wording(migrated_db: sqlite3.Connection) -> None:
    scaled = recipes.scale_ingredients(_detail(migrated_db), "4")
    assert scaled[0].display == "2 cups flour"  # factor 1 -> user's exact text
    assert scaled[1].display == "1 tbsp olive oil"


def test_scale_factor_guards_zero_base() -> None:
    assert recipes.scale_factor("4", "6") == Decimal(6) / Decimal(4)
    assert recipes.scale_factor("0", "6") == Decimal(1)


def _package_recipe(
    conn: sqlite3.Connection, *, unit: str, package_qty: str, package_unit: str | None
) -> recipes.RecipeDetail:
    units.seed_core_units(conn)
    recipe_id = recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title="Bread",
            base_servings="4",
            ingredients=[
                recipes.IngredientInput(
                    quantity_text="300", unit=unit, food="flour",
                    scaling_mode="round_to_package",
                    package_quantity_text=package_qty, package_unit=package_unit,
                )
            ],
        ),
    )
    detail = recipes.get_recipe(conn, recipe_id)
    assert detail is not None
    return detail


def test_round_to_package_converts_cross_unit(migrated_db: sqlite3.Connection) -> None:
    # 300 g bought in 1 kg packs, scaled 4->6 (x1.5 = 450 g) must round up to a full 1 kg (1000 g),
    # NOT to 450 (the bug where the '1' kg package was treated as 1 gram). Finding #2.
    detail = _package_recipe(migrated_db, unit="grams", package_qty="1", package_unit="kg")
    display = recipes.scale_ingredients(detail, "6")[0].display
    assert "1000 grams" in display
    assert "450" not in display


def test_round_to_package_same_unit_unchanged(migrated_db: sqlite3.Connection) -> None:
    # Package in the ingredient's own unit still works: 300 g in 500 g packs, x1.5 = 450 -> 500 g.
    detail = _package_recipe(migrated_db, unit="grams", package_qty="500", package_unit="grams")
    assert "500 grams" in recipes.scale_ingredients(detail, "6")[0].display


def test_round_to_package_rejects_incompatible_dimension(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    with pytest.raises(recipes.RecipeError):
        recipes.create_recipe(
            migrated_db,
            recipes.RecipeInput(
                title="Bad",
                ingredients=[
                    recipes.IngredientInput(
                        quantity_text="300", unit="grams", food="flour",
                        scaling_mode="round_to_package",
                        package_quantity_text="1", package_unit="cups",  # volume vs mass
                    )
                ],
            ),
        )
