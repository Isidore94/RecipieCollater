"""Recipe CRUD service: create/get/update/delete, status, search, and validation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services import recipes, units


def _sample() -> recipes.RecipeInput:
    return recipes.RecipeInput(
        title="Tomato Pasta",
        tldr="Simmer and reduce.",
        tier="family",
        base_servings="4",
        prep_minutes=10,
        ingredients=[
            recipes.IngredientInput(quantity_text="2", unit="cups", food="flour"),
            recipes.IngredientInput(
                quantity_text="1", unit="tbsp", food="olive oil", note="extra virgin"
            ),
            recipes.IngredientInput(
                original_text="salt to taste", food="salt", scaling_mode="to_taste"
            ),
        ],
        steps=[
            recipes.StepInput(instruction="Mix."),
            recipes.StepInput(instruction="Cook.", minutes=20),
        ],
        tags=["italian", "weeknight"],
    )


def test_create_and_get_round_trip(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    recipe_id = recipes.create_recipe(migrated_db, _sample())
    detail = recipes.get_recipe(migrated_db, recipe_id)
    assert detail is not None
    assert detail.title == "Tomato Pasta"
    assert detail.slug == "tomato-pasta"
    assert detail.status == "inbox"
    assert len(detail.ingredients) == 3
    flour = detail.ingredients[0]
    assert flour.quantity_text == "2"
    assert flour.unit_name == "cup"  # "cups" resolved to the canonical unit
    assert flour.food_name == "flour"  # food auto-created
    assert detail.ingredients[2].scaling_mode == "to_taste"
    assert [s.instruction for s in detail.steps] == ["Mix.", "Cook."]
    assert set(detail.tags) == {"italian", "weeknight"}
    made = migrated_db.execute("SELECT COUNT(*) FROM foods WHERE name = 'flour'").fetchone()[0]
    assert made == 1


def test_slug_is_unique(migrated_db: sqlite3.Connection) -> None:
    first = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="Soup"))
    second = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="Soup"))
    detail_a = recipes.get_recipe(migrated_db, first)
    detail_b = recipes.get_recipe(migrated_db, second)
    assert detail_a is not None and detail_a.slug == "soup"
    assert detail_b is not None and detail_b.slug == "soup-2"


def test_update_snapshots_revision_and_replaces_children(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    recipe_id = recipes.create_recipe(migrated_db, _sample())
    changed = recipes.RecipeInput(title="Arrabbiata", tags=["spicy"])
    assert recipes.update_recipe(migrated_db, recipe_id, changed) is True

    detail = recipes.get_recipe(migrated_db, recipe_id)
    assert detail is not None
    assert detail.title == "Arrabbiata"
    assert detail.slug == "tomato-pasta"  # slug is stable across edits
    assert set(detail.tags) == {"spicy"}
    assert len(detail.ingredients) == 0  # children replaced wholesale

    row = migrated_db.execute(
        "SELECT payload FROM recipe_revisions WHERE recipe_id = ?", (recipe_id,)
    ).fetchone()
    assert row is not None
    prior = json.loads(row["payload"])
    assert prior["title"] == "Tomato Pasta"
    assert len(prior["ingredients"]) == 3


def test_update_missing_returns_false(migrated_db: sqlite3.Connection) -> None:
    assert recipes.update_recipe(migrated_db, 999, recipes.RecipeInput(title="x")) is False


def test_set_status_promotes_to_cookbook(migrated_db: sqlite3.Connection) -> None:
    recipe_id = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="Soup"))
    assert recipes.set_status(migrated_db, recipe_id, "cookbook") is True
    row = migrated_db.execute(
        "SELECT status, promoted_at FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    assert row["status"] == "cookbook"
    assert row["promoted_at"] is not None


def test_set_status_rejects_unknown(migrated_db: sqlite3.Connection) -> None:
    recipe_id = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="Soup"))
    with pytest.raises(recipes.RecipeError):
        recipes.set_status(migrated_db, recipe_id, "deleted")


def test_delete(migrated_db: sqlite3.Connection) -> None:
    recipe_id = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="Soup"))
    assert recipes.delete_recipe(migrated_db, recipe_id) is True
    assert recipes.get_recipe(migrated_db, recipe_id) is None
    assert recipes.delete_recipe(migrated_db, recipe_id) is False


def test_list_by_status_and_search(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    pasta = recipes.create_recipe(migrated_db, _sample())  # inbox
    soup = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="Chicken Soup"))
    recipes.set_status(migrated_db, soup, "cookbook")

    assert [s.id for s in recipes.list_recipes(migrated_db, status="inbox")] == [pasta]
    assert [s.id for s in recipes.list_recipes(migrated_db, status="cookbook")] == [soup]
    assert [s.id for s in recipes.list_recipes(migrated_db, query="pasta")] == [pasta]
    assert [s.id for s in recipes.list_recipes(migrated_db, query="chicken")] == [soup]
    assert recipes.list_recipes(migrated_db, query="zzz-nothing") == []


def test_validation_rejects_bad_input(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    bad_inputs = [
        recipes.RecipeInput(title="   "),
        recipes.RecipeInput(title="X", tier="deluxe"),
        recipes.RecipeInput(title="X", base_servings="0"),
        recipes.RecipeInput(
            title="X",
            ingredients=[recipes.IngredientInput(quantity_text="2", unit="florbs", food="flour")],
        ),
        recipes.RecipeInput(
            title="X", ingredients=[recipes.IngredientInput(food="flour", scaling_mode="bogus")]
        ),
        recipes.RecipeInput(
            title="X",
            ingredients=[recipes.IngredientInput(food="flour", scaling_mode="round_to_package")],
        ),
    ]
    for bad in bad_inputs:
        with pytest.raises(recipes.RecipeError):
            recipes.create_recipe(migrated_db, bad)
    # A validation failure must not leave a half-written recipe behind.
    assert migrated_db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0
