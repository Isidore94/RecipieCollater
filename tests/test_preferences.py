"""Household preferences: hard/soft lists, scalars, and the hard-constraint recipe filter."""

from __future__ import annotations

import sqlite3

import pytest

from app.services import preferences, recipes
from app.services.units import seed_core_units


def test_add_dedupes_and_lists(migrated_db: sqlite3.Connection) -> None:
    preferences.add_preference(migrated_db, "allergy", "Peanut")
    preferences.add_preference(migrated_db, "allergy", "Peanut")  # UNIQUE(kind,value)
    preferences.add_preference(migrated_db, "dislike", "olives")
    prefs = preferences.load(migrated_db)
    assert prefs.allergy == ["Peanut"]
    assert prefs.dislike == ["olives"]
    assert prefs.hard_terms == ["peanut"]
    with pytest.raises(preferences.PreferenceError):
        preferences.add_preference(migrated_db, "nonsense", "x")


def test_scalars_set_update_clear(migrated_db: sqlite3.Connection) -> None:
    preferences.set_scalar(migrated_db, "max_weekday_minutes", "45")
    assert preferences.load(migrated_db).scalars["max_weekday_minutes"] == "45"
    preferences.set_scalar(migrated_db, "max_weekday_minutes", "30")  # update
    assert preferences.load(migrated_db).scalars["max_weekday_minutes"] == "30"
    preferences.set_scalar(migrated_db, "max_weekday_minutes", "")  # clear
    assert "max_weekday_minutes" not in preferences.load(migrated_db).scalars
    with pytest.raises(preferences.PreferenceError):
        preferences.set_scalar(migrated_db, "bogus", "1")


def test_hard_filter_matches_whole_words(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    peanut = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Satay", base_servings="4",
            ingredients=[
                recipes.IngredientInput(quantity_text="2", unit="tbsp", food="peanut butter")
            ],
        ),
    )
    safe = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Rice", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="1", unit="cup", food="rice")],
        ),
    )
    assert preferences.recipe_violates_hard(migrated_db, peanut, ["peanut"]) == "peanut"
    assert preferences.recipe_violates_hard(migrated_db, safe, ["peanut"]) is None
    assert preferences.recipe_violates_hard(migrated_db, safe, []) is None


def test_hard_filter_matches_plurals(migrated_db: sqlite3.Connection) -> None:
    """Review fix: a stored singular allergen must catch the plural in an ingredient (and the
    reverse), including free-text ingredients with no linked food."""
    seed_core_units(migrated_db)
    # free-text ingredient (food_id stays null) written in the plural
    rid = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Satay", base_servings="4",
            ingredients=[recipes.IngredientInput(
                original_text="1/2 cup peanuts, chopped", food=None
            )],
        ),
    )
    assert preferences.recipe_violates_hard(migrated_db, rid, ["peanut"]) == "peanut"
    # multi-word allergen matches as a phrase
    rid2 = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Stirfry", base_servings="4",
            ingredients=[recipes.IngredientInput(original_text="2 tbsp soy sauce", food=None)],
        ),
    )
    assert preferences.recipe_violates_hard(migrated_db, rid2, ["soy sauce"]) == "soy sauce"
    # 'nut' must NOT trip 'nutmeg' (whole-stem, not substring, for single words)
    rid3 = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Spice", base_servings="4",
            ingredients=[recipes.IngredientInput(original_text="1 tsp nutmeg", food=None)],
        ),
    )
    assert preferences.recipe_violates_hard(migrated_db, rid3, ["nut"]) is None
