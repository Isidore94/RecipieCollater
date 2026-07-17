"""Ephemeral serving scaler: exact scaling, kitchen fractions, per-mode behaviour."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

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
