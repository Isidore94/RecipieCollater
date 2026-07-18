"""Food ontology upkeep: purchase info, aisles, families, merge, and learned substitutions."""

from __future__ import annotations

import sqlite3

import pytest

from app.services import foods, pantry, recipes
from app.services.units import seed_core_units


def _food_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    assert row is not None, f"no food {name!r}"
    return int(row["id"])


def _recipe(conn: sqlite3.Connection, food: str, qty: str = "200", unit: str = "grams") -> int:
    return recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=f"{food} dish", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text=qty, unit=unit, food=food)],
        ),
    )


def test_set_purchase_and_get(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "flour")
    fid = _food_id(migrated_db, "flour")
    foods.set_purchase(migrated_db, fid, quantity_text="2", unit="kg", label="bag")
    info = foods.get_purchase(migrated_db, fid)
    assert info is not None
    assert info.label == "bag" and info.quantity_text == "2"
    assert info.canonical == 2_000_000  # 2 kg in mg
    assert info.package_word(2) == "bags"
    # all-blank clears it
    foods.set_purchase(migrated_db, fid, quantity_text=None, unit=None, label=None)
    assert foods.get_purchase(migrated_db, fid) is None


def test_set_purchase_rejects_bad_input(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "flour")
    fid = _food_id(migrated_db, "flour")
    with pytest.raises(foods.FoodError):
        foods.set_purchase(migrated_db, fid, quantity_text="2", unit="blorps", label=None)
    with pytest.raises(foods.FoodError):
        foods.set_purchase(migrated_db, fid, quantity_text="0", unit="kg", label=None)


def test_parent_cycle_guard_and_family(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "chicken")
    _recipe(migrated_db, "chicken breast")
    _recipe(migrated_db, "chicken thigh")
    chicken = _food_id(migrated_db, "chicken")
    breast = _food_id(migrated_db, "chicken breast")
    thigh = _food_id(migrated_db, "chicken thigh")
    foods.set_parent(migrated_db, breast, chicken)
    foods.set_parent(migrated_db, thigh, chicken)
    with pytest.raises(foods.FoodError):
        foods.set_parent(migrated_db, chicken, breast)  # would create a cycle
    with pytest.raises(foods.FoodError):
        foods.set_parent(migrated_db, chicken, chicken)
    # the family is visible from every member
    assert foods.family_ids(migrated_db, breast) == {chicken, breast, thigh}
    assert foods.family_ids(migrated_db, chicken) == {chicken, breast, thigh}


def test_merge_rewrites_references_and_aliases(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    keep_recipe = _recipe(migrated_db, "chicken breast")
    dupe_recipe = _recipe(migrated_db, "chicken breasts")  # the classic plural duplicate
    keep = _food_id(migrated_db, "chicken breast")
    dupe = _food_id(migrated_db, "chicken breasts")
    loc = pantry.create_location(migrated_db, "Freezer")
    item = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Chicken breasts", location_id=loc, food="chicken breasts"
        ),
    )
    foods.set_category(migrated_db, dupe, "Meat")

    foods.merge_foods(migrated_db, dupe, keep)

    # every reference now points at the kept food
    for recipe_id in (keep_recipe, dupe_recipe):
        row = migrated_db.execute(
            "SELECT food_id FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()
        assert row["food_id"] == keep
    fetched = pantry.get_item(migrated_db, item)
    assert fetched is not None and fetched.food_id == keep
    # the dupe's name became an alias, so the next import resolves straight to the kept food
    alias = migrated_db.execute(
        "SELECT food_id FROM food_aliases WHERE alias = 'chicken breasts'"
    ).fetchone()
    assert alias is not None and alias["food_id"] == keep
    # metadata carried onto the target where the target had none
    row = migrated_db.execute("SELECT category FROM foods WHERE id = ?", (keep,)).fetchone()
    assert row["category"] == "Meat"
    assert migrated_db.execute("SELECT 1 FROM foods WHERE id = ?", (dupe,)).fetchone() is None
    with pytest.raises(foods.FoodError):
        foods.merge_foods(migrated_db, keep, keep)


def test_record_substitute_learns_and_counts(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "sour cream")
    _recipe(migrated_db, "greek yogurt")
    sour = _food_id(migrated_db, "sour cream")
    yogurt = _food_id(migrated_db, "greek yogurt")

    foods.record_substitute(migrated_db, sour, "greek yogurt")
    foods.record_substitute(migrated_db, sour, "Greek Yogurt")  # same sub, case-insensitive

    subs = foods.substitutes_for(migrated_db, sour)
    assert len(subs) == 1
    assert subs[0].times_used == 2
    assert subs[0].substitute_food_id == yogurt  # resolved to the known food
    with pytest.raises(foods.FoodError):
        foods.record_substitute(migrated_db, sour, "   ")


def test_list_foods_pending_first_with_counts(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "flour")
    recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Imported", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="1", unit="each", food="jicama")],
        ),
        food_status="pending",
    )
    listing = foods.list_foods(migrated_db)
    assert listing[0].name == "jicama" and listing[0].status == "pending"
    flour = next(f for f in listing if f.name == "flour")
    assert flour.recipe_count == 1
    foods.confirm_food(migrated_db, listing[0].id)
    assert all(f.status == "confirmed" for f in foods.list_foods(migrated_db))
    assert [f.name for f in foods.list_foods(migrated_db, query="jic")] == ["jicama"]


def test_merge_never_leaves_a_self_parent(migrated_db: sqlite3.Connection) -> None:
    """Merging a family root into one of its children must not make the child its own parent."""
    seed_core_units(migrated_db)
    _recipe(migrated_db, "chicken")
    _recipe(migrated_db, "chicken breast")
    chicken = _food_id(migrated_db, "chicken")
    breast = _food_id(migrated_db, "chicken breast")
    foods.set_parent(migrated_db, breast, chicken)

    foods.merge_foods(migrated_db, chicken, breast)  # merge the ROOT into the child

    row = migrated_db.execute(
        "SELECT parent_food_id FROM foods WHERE id = ?", (breast,)
    ).fetchone()
    assert row["parent_food_id"] is None  # not itself
    foods.set_parent(migrated_db, breast, None)  # and set_parent still works
