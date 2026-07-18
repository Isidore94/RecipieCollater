"""Shopping list: manual adds, recipe adds (missing-only + merge), staples, aisles, export."""

from __future__ import annotations

import sqlite3

from app.services import pantry, recipes, shopping
from app.services.units import seed_core_units


def _recipe(conn: sqlite3.Connection, qty: str, unit: str, food: str) -> int:
    return recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=food.title(), base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text=qty, unit=unit, food=food)],
        ),
    )


def _exact_pantry(conn: sqlite3.Connection, food: str, qty: str, unit: str) -> int:
    loc = pantry.create_location(conn, "Pantry")
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(
            display_name=food.title(), location_id=loc, quantity_mode="exact",
            food=food, quantity_text=qty, unit=unit,
        ),
    )


def test_add_manual(migrated_db: sqlite3.Connection) -> None:
    lst = shopping.active_list(migrated_db)
    shopping.add_manual(migrated_db, lst, "paper towels")
    items = shopping.list_items(migrated_db, lst)
    assert len(items) == 1
    assert items[0].display_text == "paper towels" and items[0].is_manual is True


def test_add_from_recipe_subtracts_pantry(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "200", "grams", "flour")
    _exact_pantry(migrated_db, "flour", "50", "grams")  # already have 50 g
    lst = shopping.active_list(migrated_db)
    added = shopping.add_from_recipe(migrated_db, lst, rid)
    assert added == 1
    item = shopping.list_items(migrated_db, lst)[0]
    assert item.quantity_text == "150"  # 200 needed - 50 on hand


def test_add_from_recipe_skips_when_stocked(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "200", "grams", "flour")
    _exact_pantry(migrated_db, "flour", "500", "grams")  # plenty
    lst = shopping.active_list(migrated_db)
    assert shopping.add_from_recipe(migrated_db, lst, rid) == 0
    assert shopping.list_items(migrated_db, lst) == []


def test_add_from_recipe_merges_same_food(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    a = _recipe(migrated_db, "200", "grams", "flour")
    b = _recipe(migrated_db, "100", "grams", "flour")
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, a)
    shopping.add_from_recipe(migrated_db, lst, b)
    items = shopping.list_items(migrated_db, lst)
    assert len(items) == 1  # merged into one flour line
    assert items[0].quantity_text == "300"


def test_gauge_pantry_item_covers_recipe(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "200", "grams", "flour")
    loc = pantry.create_location(migrated_db, "Pantry")
    pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Flour", location_id=loc, quantity_mode="gauge", food="flour"
        ),
    )  # gauge 'full' -> treated as covered
    lst = shopping.active_list(migrated_db)
    assert shopping.add_from_recipe(migrated_db, lst, rid) == 0


def test_add_staples(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    item = _exact_pantry(migrated_db, "rice", "0", "grams")
    pantry.set_staple(migrated_db, item, is_staple=True, min_quantity_text="500")  # 0 < 500 -> low
    lst = shopping.active_list(migrated_db)
    assert shopping.add_staples(migrated_db, lst) == 1
    assert shopping.list_items(migrated_db, lst)[0].display_text == "Rice"
    # calling again does not duplicate an already-listed staple
    assert shopping.add_staples(migrated_db, lst) == 0


def test_toggle_and_clear_checked(migrated_db: sqlite3.Connection) -> None:
    lst = shopping.active_list(migrated_db)
    item_id = shopping.add_manual(migrated_db, lst, "milk")
    assert shopping.toggle(migrated_db, item_id) is True
    assert shopping.counts(migrated_db, lst) == (0, 1)  # 0 remaining of 1
    assert shopping.clear_checked(migrated_db, lst) == 1
    assert shopping.list_items(migrated_db, lst) == []


def test_grouped_by_aisle_and_text_export(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "2", "each", "eggs")
    migrated_db.execute("UPDATE foods SET category = 'Dairy' WHERE name = 'eggs'")
    migrated_db.commit()
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    shopping.add_manual(migrated_db, lst, "napkins")

    aisles = dict(shopping.grouped(migrated_db, lst))
    assert "Dairy" in aisles and "Other" in aisles
    text = shopping.to_text(migrated_db, lst)
    assert "Dairy:" in text and "eggs" in text


def test_reminders_text_is_plain_lines(migrated_db: sqlite3.Connection) -> None:
    lst = shopping.active_list(migrated_db)
    shopping.add_manual(migrated_db, lst, "paper towels")
    checked = shopping.add_manual(migrated_db, lst, "milk")
    shopping.add_manual(migrated_db, lst, "eggs")
    shopping.toggle(migrated_db, checked)  # milk is bought -> excluded

    text = shopping.to_reminders_text(migrated_db, lst)
    lines = text.splitlines()
    assert "paper towels" in lines and "eggs" in lines
    assert "milk" not in lines  # checked-off items are not re-added to Reminders
    assert not any(line.endswith(":") for line in lines)  # no aisle headers, just item names
