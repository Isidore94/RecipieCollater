"""How a food should be tracked: counted, gauged, or have/out.

The pantry used to default every new item to the gauge, so countable things arrived as "half an
avocado" — an amount nobody means. These cases are the vocabulary a real household actually
types in.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.services import pantry, quantity_mode
from app.services.units import seed_core_units


@pytest.mark.parametrize(
    "name",
    [
        "avocado", "avocados", "2 avocados", "eggs", "lemon", "red onions", "bell peppers",
        "potatoes", "sweet potatoes", "tomatoes", "cherry tomatoes", "tinned tomatoes",
        "tin of chopped tomatoes", "jar of pesto", "chicken breasts", "sausages",
        "loaf of bread", "bananas", "peaches", "courgettes",
    ],
)
def test_countable_things_are_counted(name: str) -> None:
    assert quantity_mode.infer(name) == quantity_mode.EXACT


@pytest.mark.parametrize(
    "name",
    [
        "flour", "plain flour", "rice", "pasta", "olive oil", "sugar", "milk", "coconut milk",
        "cheddar cheese", "minced beef", "greek yoghurt", "porridge oats", "maple syrup",
    ],
)
def test_bulk_staples_use_the_gauge(name: str) -> None:
    assert quantity_mode.infer(name) == quantity_mode.GAUGE


@pytest.mark.parametrize(
    "name",
    [
        "cumin", "ground cumin", "black pepper", "chilli powder", "soy sauce", "baking powder",
        "vanilla extract", "dijon mustard", "balsamic vinegar", "ketchup",
    ],
)
def test_condiments_are_have_or_out(name: str) -> None:
    assert quantity_mode.infer(name) == quantity_mode.BINARY


def test_the_longest_phrase_decides() -> None:
    """'coconut' is counted and 'chilli' is counted, but the fuller phrase is what she meant."""
    assert quantity_mode.infer("coconut") == quantity_mode.EXACT
    assert quantity_mode.infer("coconut milk") == quantity_mode.GAUGE
    assert quantity_mode.infer("chilli") == quantity_mode.EXACT
    assert quantity_mode.infer("chilli powder") == quantity_mode.BINARY


def test_whole_word_matching_only() -> None:
    """'oil' must not match 'boiled', and 'can' must not match 'pecan'."""
    assert quantity_mode.infer("boiled ham") != quantity_mode.EXACT
    assert quantity_mode.infer("pecans") == quantity_mode.GAUGE


def test_unknown_food_falls_back_to_the_gauge() -> None:
    """The least annoying thing to be wrong about: no number, no decision."""
    assert quantity_mode.infer("zzzsomething") == quantity_mode.GAUGE


def test_aisle_is_used_when_the_name_says_nothing() -> None:
    assert quantity_mode.infer("mystery item", category="Produce") == quantity_mode.EXACT
    assert quantity_mode.infer("mystery item", category="Spices") == quantity_mode.BINARY


def test_a_recorded_choice_beats_the_inference(migrated_db: sqlite3.Connection) -> None:
    """A household that counts its rice must not be overruled every time."""
    food_id = int(
        migrated_db.execute("INSERT INTO foods (name) VALUES ('rice')").lastrowid or 0
    )
    assert quantity_mode.suggest(migrated_db, "rice") == quantity_mode.GAUGE

    quantity_mode.remember(migrated_db, food_id, quantity_mode.EXACT)
    assert quantity_mode.suggest(migrated_db, "rice") == quantity_mode.EXACT


def test_adding_an_avocado_gives_a_count_not_a_gauge(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Fruit bowl")
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(display_name="Avocados", location_id=loc, quantity_mode="auto"),
    )
    item = pantry.get_item(migrated_db, item_id)
    assert item is not None
    assert item.quantity_mode == "exact"
    # No number typed means one of them, rather than a demand for a number.
    assert item.quantity_text == "1"


def test_adding_flour_still_gives_a_gauge(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Cupboard")
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(display_name="Plain flour", location_id=loc, quantity_mode="auto"),
    )
    item = pantry.get_item(migrated_db, item_id)
    assert item is not None and item.quantity_mode == "gauge" and item.gauge == "full"


def test_an_explicit_choice_is_remembered_for_next_time(
    migrated_db: sqlite3.Connection,
) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Cupboard")
    pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(display_name="Rice", location_id=loc, quantity_mode="exact",
                               quantity_text="2", unit="each"),
    )
    # The same food added again on 'auto' now follows the household's own answer.
    assert quantity_mode.suggest(migrated_db, "Rice") == quantity_mode.EXACT


def test_switching_an_item_to_a_count_carries_the_level_over(
    migrated_db: sqlite3.Connection,
) -> None:
    """A mis-guessed avocado must be correctable without deleting and re-adding it."""
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Fruit bowl")
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(display_name="Avocado", location_id=loc, quantity_mode="gauge"),
    )
    pantry.set_gauge(migrated_db, item_id, "low")

    pantry.set_quantity_mode(migrated_db, item_id, "exact")
    item = pantry.get_item(migrated_db, item_id)
    assert item is not None
    assert item.quantity_mode == "exact"
    assert item.quantity_text == "1"  # "low" carries across as one left, not as a reset


def test_switching_a_count_to_a_gauge_carries_the_level_over(
    migrated_db: sqlite3.Connection,
) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Cupboard")
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(display_name="Tins of beans", location_id=loc,
                               quantity_mode="exact", quantity_text="0", unit="each"),
    )
    pantry.set_quantity_mode(migrated_db, item_id, "gauge")
    item = pantry.get_item(migrated_db, item_id)
    assert item is not None and item.quantity_mode == "gauge" and item.gauge == "out"
