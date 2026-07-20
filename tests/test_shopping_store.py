"""Phase 4.6 shopping: store-language labels, staple lane, no silent drops, trips, restock."""

from __future__ import annotations

import sqlite3

from app.services import foods, pantry, recipes, shopping
from app.services.units import seed_core_units


def _recipe(
    conn: sqlite3.Connection, ingredients: list[recipes.IngredientInput], title: str = "Dish"
) -> int:
    return recipes.create_recipe(
        conn, recipes.RecipeInput(title=title, base_servings="4", ingredients=ingredients)
    )


def _ing(qty: str | None, unit: str | None, food: str | None, **kw: str) -> recipes.IngredientInput:
    return recipes.IngredientInput(quantity_text=qty, unit=unit, food=food, **kw)


def _food_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    assert row is not None
    return int(row["id"])


def _gauge_item(conn: sqlite3.Connection, food: str, gauge: str = "full") -> int:
    loc = pantry.create_location(conn, "Pantry")
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(
            display_name=food.title(), location_id=loc, quantity_mode="gauge",
            food=food, gauge=gauge,
        ),
    )


def _exact_item(conn: sqlite3.Connection, food: str, qty: str, unit: str) -> int:
    loc = pantry.create_location(conn, "Pantry")
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(
            display_name=food.title(), location_id=loc, quantity_mode="exact",
            food=food, quantity_text=qty, unit=unit,
        ),
    )


# ---- purchase-unit display -------------------------------------------------------------


def test_measured_line_ceilings_to_purchase_packages(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("1500", "grams", "flour")])
    foods.set_purchase(migrated_db, _food_id(migrated_db, "flour"), quantity_text="1",
                       unit="kg", label="bag")
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    item = shopping.list_items(migrated_db, lst)[0]
    assert item.packages == 2  # 1.5 kg needed -> 2 x 1 kg bags
    assert "2 bags" in item.label
    assert "need 1500" in item.label  # the cooking need stays visible as detail


def test_purchase_dimension_mismatch_falls_back_plain(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("2", "cups", "flour")])  # volume
    foods.set_purchase(migrated_db, _food_id(migrated_db, "flour"), quantity_text="1",
                       unit="kg", label="bag")  # mass - cannot bridge without density
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    item = shopping.list_items(migrated_db, lst)[0]
    assert item.packages is None
    assert item.label.startswith("2 cup")


def test_wants_purchase_info_prompt(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    assert shopping.list_items(migrated_db, lst)[0].wants_purchase_info is True
    shopping.set_purchase_info(
        migrated_db, _food_id(migrated_db, "flour"), quantity_text="1", unit="kg", label="bag"
    )
    assert shopping.list_items(migrated_db, lst)[0].wants_purchase_info is False


# ---- staple lane -----------------------------------------------------------------------


def test_empty_gauge_food_lands_without_cooking_amounts(migrated_db: sqlite3.Connection) -> None:
    """The user's own example: a recipe needs 1/4 cup of flour, but nobody buys 1/4 cup."""
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("1/4", "cup", "flour")])
    _gauge_item(migrated_db, "flour", gauge="out")
    lst = shopping.active_list(migrated_db)
    outcome = shopping.add_from_recipe(migrated_db, lst, rid)
    assert outcome.added == 1
    item = shopping.list_items(migrated_db, lst)[0]
    assert item.quantity_text is None  # no "1/4 cup" on the list
    assert "1/4" not in item.label
    # a second recipe wanting flour merges into the same quantity-less line
    rid2 = _recipe(migrated_db, [_ing("2", "cups", "flour")], title="Other")
    shopping.add_from_recipe(migrated_db, lst, rid2)
    assert len(shopping.list_items(migrated_db, lst)) == 1


def test_staple_line_renders_purchase_words(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("1/4", "cup", "flour")])
    _gauge_item(migrated_db, "flour", gauge="out")
    foods.set_purchase(migrated_db, _food_id(migrated_db, "flour"), quantity_text="2",
                       unit="kg", label="bag")
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    assert "1 bag" in shopping.list_items(migrated_db, lst)[0].label


# ---- nothing is silently dropped -------------------------------------------------------


def test_unmeasured_ingredient_becomes_check_line(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(
        migrated_db,
        [recipes.IngredientInput(original_text="a knob of butter", food="butter")],
    )
    lst = shopping.active_list(migrated_db)
    outcome = shopping.add_from_recipe(migrated_db, lst, rid)
    assert outcome.added == 1 and outcome.to_check == 1
    item = shopping.list_items(migrated_db, lst)[0]
    assert item.needs_check is True
    assert "check the amount" in item.label
    # re-adding the same recipe doesn't duplicate the check line
    shopping.add_from_recipe(migrated_db, lst, rid)
    assert len(shopping.list_items(migrated_db, lst)) == 1


def test_fixed_scaling_lines_do_shop(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("1", "each", "bay leaf", scaling_mode="fixed")])
    lst = shopping.active_list(migrated_db)
    outcome = shopping.add_from_recipe(migrated_db, lst, rid, servings="8")  # factor 2
    assert outcome.added == 1
    assert shopping.list_items(migrated_db, lst)[0].quantity_text == "1"  # fixed never scales


def test_to_taste_reported_not_listed(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(
        migrated_db,
        [recipes.IngredientInput(original_text="salt to taste", food="salt",
                                 scaling_mode="to_taste")],
    )
    lines = shopping.plan_recipe(migrated_db, rid)
    assert [line.kind for line in lines] == ["to_taste"]
    lst = shopping.active_list(migrated_db)
    assert shopping.add_from_recipe(migrated_db, lst, rid).added == 0


# ---- provenance ------------------------------------------------------------------------


def test_sources_by_item_names_the_recipe(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")], title="Sunday Bread")
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    item = shopping.list_items(migrated_db, lst)[0]
    assert shopping.sources_by_item(migrated_db, lst)[item.id] == ["for Sunday Bread"]


# ---- the trip builder ------------------------------------------------------------------


def test_trip_subtracts_pantry_once_across_recipes(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    a = _recipe(migrated_db, [_ing("200", "grams", "flour")], title="A")
    b = _recipe(migrated_db, [_ing("200", "grams", "flour")], title="B")
    _exact_item(migrated_db, "flour", "300", "grams")
    preview = shopping.build_trip(migrated_db, [(a, None), (b, None)])
    assert len(preview.to_buy) == 1
    line = preview.to_buy[0]
    assert line.quantity_text == "100"  # 400 total - 300 on hand, subtracted ONCE
    assert set(line.recipe_titles) == {"A", "B"}


def test_trip_covered_and_apply_with_exclusions(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    a = _recipe(migrated_db, [_ing("200", "grams", "flour"), _ing("2", "each", "eggs")], title="A")
    _exact_item(migrated_db, "flour", "500", "grams")  # plenty -> covered
    preview = shopping.build_trip(migrated_db, [(a, None)])
    assert preview.covered == ["flour"]
    assert [line.display_text for line in preview.to_buy] == ["eggs"]

    lst = shopping.active_list(migrated_db)
    excluded = {line.key for line in preview.to_buy}
    assert shopping.apply_trip(migrated_db, lst, [(a, None)], exclude=excluded) == 0
    assert shopping.apply_trip(migrated_db, lst, [(a, None)]) == 1
    assert shopping.list_items(migrated_db, lst)[0].display_text == "eggs"


# ---- done shopping -> restock ----------------------------------------------------------


def test_restock_gauge_and_exact_and_clear(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    flour_item = _gauge_item(migrated_db, "flour", gauge="out")
    rid = _recipe(migrated_db, [_ing("300", "grams", "rice"), _ing("1/4", "cup", "flour")])
    _exact_item(migrated_db, "rice", "100", "grams")
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)  # rice 200 g line + flour staple line

    for item in shopping.list_items(migrated_db, lst):
        shopping.toggle(migrated_db, item.id)  # bought everything

    candidates = shopping.restock_candidates(migrated_db, lst)
    by_name = {c.display_text: c for c in candidates}
    assert by_name["flour"].action_text == "mark full"
    assert by_name["rice"].action_text is not None and by_name["rice"].action_text.startswith("+")

    shopping.apply_restock(
        migrated_db, lst, restock_item_ids={c.item_id for c in candidates},
        create_item_ids=set(), create_location_id=None,
    )
    flour = pantry.get_item(migrated_db, flour_item)
    assert flour is not None and flour.gauge == "full"
    rice = pantry.items_for_food(migrated_db, _food_id(migrated_db, "rice"))[0]
    assert rice.quantity_text == "300"  # 100 on hand + 200 bought
    assert shopping.list_items(migrated_db, lst) == []  # checked lines cleared


def test_restock_can_create_new_pantry_item(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Fridge")
    rid = _recipe(migrated_db, [_ing("200", "ml", "milk")])
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    item = shopping.list_items(migrated_db, lst)[0]
    shopping.toggle(migrated_db, item.id)

    candidates = shopping.restock_candidates(migrated_db, lst)
    assert candidates[0].can_create is True
    shopping.apply_restock(
        migrated_db, lst, restock_item_ids=set(),
        create_item_ids={candidates[0].item_id}, create_location_id=loc,
    )
    created = pantry.items_for_food(migrated_db, _food_id(migrated_db, "milk"))
    assert len(created) == 1 and created[0].quantity_mode == "gauge" and created[0].gauge == "full"


def test_restock_per_line_location_overrides_default(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    fridge = pantry.create_location(migrated_db, "Fridge")
    freezer = pantry.create_location(migrated_db, "Freezer", is_freezer=True)
    rid = _recipe(migrated_db, [_ing("200", "ml", "milk"), _ing("1", "each", "peas")])
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    for item in shopping.list_items(migrated_db, lst):
        shopping.toggle(migrated_db, item.id)

    candidates = shopping.restock_candidates(migrated_db, lst)
    by_food = {c.food_id: c.item_id for c in candidates}
    peas_item = by_food[_food_id(migrated_db, "peas")]
    shopping.apply_restock(
        migrated_db, lst, restock_item_ids=set(),
        create_item_ids={c.item_id for c in candidates}, create_location_id=fridge,
        create_locations={peas_item: freezer},
    )
    milk = pantry.items_for_food(migrated_db, _food_id(migrated_db, "milk"))
    peas = pantry.items_for_food(migrated_db, _food_id(migrated_db, "peas"))
    assert milk and milk[0].location_id == fridge
    assert peas and peas[0].location_id == freezer


# ---- adversarial-review regressions ----------------------------------------------------


def test_same_food_twice_in_one_recipe_claims_stock_once(
    migrated_db: sqlite3.Connection,
) -> None:
    """Two flour lines must not both count the same pantry flour (under-buying)."""
    seed_core_units(migrated_db)
    rid = _recipe(
        migrated_db,
        [_ing("200", "grams", "flour"), _ing("100", "grams", "flour")],
    )
    _exact_item(migrated_db, "flour", "150", "grams")
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)
    items = shopping.list_items(migrated_db, lst)
    assert len(items) == 1  # merged
    assert items[0].quantity_text == "150"  # 300 total need - 150 on hand, claimed once


def test_trip_keys_distinct_for_one_food_in_two_dimensions(
    migrated_db: sqlite3.Connection,
) -> None:
    seed_core_units(migrated_db)
    a = _recipe(migrated_db, [_ing("200", "grams", "flour")], title="ByMass")
    b = _recipe(migrated_db, [_ing("1", "cup", "flour")], title="ByVolume")
    preview = shopping.build_trip(migrated_db, [(a, None), (b, None)])
    keys = [line.key for line in preview.to_buy]
    assert len(keys) == 2 and len(set(keys)) == 2  # no collision
    # untick just the mass line: only the volume line lands
    lst = shopping.active_list(migrated_db)
    mass_key = next(line.key for line in preview.to_buy if line.unit_name == "gram")
    added = shopping.apply_trip(migrated_db, lst, [(a, None), (b, None)], exclude={mass_key})
    assert added == 1
    assert shopping.list_items(migrated_db, lst)[0].unit_name == "cup"


def test_trip_provenance_names_every_contributing_recipe(
    migrated_db: sqlite3.Connection,
) -> None:
    seed_core_units(migrated_db)
    a = _recipe(migrated_db, [_ing("200", "grams", "flour")], title="Bread")
    b = _recipe(migrated_db, [_ing("100", "grams", "flour")], title="Roux")
    lst = shopping.active_list(migrated_db)
    shopping.apply_trip(migrated_db, lst, [(a, None), (b, None)])
    item = shopping.list_items(migrated_db, lst)[0]
    labels = set(shopping.sources_by_item(migrated_db, lst)[item.id])
    assert labels == {"for Bread", "for Roux"}


def test_restock_clear_scoped_to_presented_lines(migrated_db: sqlite3.Connection) -> None:
    lst = shopping.active_list(migrated_db)
    seen = shopping.add_manual(migrated_db, lst, "milk")
    late = shopping.add_manual(migrated_db, lst, "eggs")
    shopping.toggle(migrated_db, seen)
    shopping.toggle(migrated_db, late)  # checked AFTER the review form rendered
    shopping.apply_restock(
        migrated_db, lst, restock_item_ids=set(), create_item_ids=set(),
        create_location_id=None, clear_item_ids={seen},
    )
    remaining = shopping.list_items(migrated_db, lst)
    assert [i.display_text for i in remaining] == ["eggs"]  # the unseen line survived
