"""Meal-plan week board: entries, plan->shopping, saved menus, iCal."""

from __future__ import annotations

import sqlite3
from datetime import date

from fastapi.testclient import TestClient

from app.services import planning, recipes, shopping
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN

_MON = date(2026, 7, 20)  # a Monday


def _recipe(conn: sqlite3.Connection, title: str, food: str = "flour") -> int:
    rid = recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=title, base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="200", unit="grams", food=food)],
        ),
    )
    recipes.set_status(conn, rid, "cookbook")
    return rid


def test_week_start_is_monday() -> None:
    assert planning.week_start(date(2026, 7, 22)) == _MON  # Wed -> that Monday
    assert planning.week_start(_MON) == _MON


def test_add_entries_and_board(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Bread")
    planning.add_recipe_entry(migrated_db, _MON.isoformat(), rid, servings_text="6")
    planning.add_note_entry(migrated_db, _MON.isoformat(), "leftovers", slot="lunch")
    board = planning.week_board(migrated_db, _MON)
    assert board[0].weekday == "Mon"
    labels = [(e.label, e.entry_type, e.servings_text) for e in board[0].entries]
    assert ("Bread", "recipe", "6") in labels
    assert ("leftovers", "note", None) in labels
    assert all(len(col.entries) == 0 for col in board[1:])


def test_recipe_entry_defaults_servings_to_base(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Soup")
    planning.add_recipe_entry(migrated_db, _MON.isoformat(), rid)
    entry = planning.week_board(migrated_db, _MON)[0].entries[0]
    assert entry.servings_text == "4"


def test_plan_to_shopping_uses_trip_builder(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    a = _recipe(migrated_db, "A", food="flour")
    b = _recipe(migrated_db, "B", food="flour")  # same food across two meals -> aggregates once
    planning.add_recipe_entry(migrated_db, _MON.isoformat(), a)
    planning.add_recipe_entry(migrated_db, (date(2026, 7, 21)).isoformat(), b)
    added = planning.plan_to_shopping(migrated_db, _MON)
    assert added == 1
    items = shopping.list_items(migrated_db, shopping.active_list(migrated_db))
    assert len(items) == 1 and items[0].quantity_text == "400"  # 200 + 200


def test_saved_menu_roundtrip(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Taco")
    planning.add_recipe_entry(migrated_db, _MON.isoformat(), rid, slot="dinner")
    menu_id = planning.save_week_as_menu(migrated_db, _MON, "Standard week")
    assert [m.name for m in planning.list_menus(migrated_db)] == ["Standard week"]
    # apply to a DIFFERENT week; day_index is preserved (Monday -> Monday)
    other = date(2026, 7, 27)
    added = planning.apply_menu_to_week(migrated_db, menu_id, other, user_id=None)
    assert added == 1
    assert planning.week_board(migrated_db, other)[0].entries[0].recipe_id == rid


def test_saved_menu_skips_deleted_recipe(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Gone")
    planning.add_recipe_entry(migrated_db, _MON.isoformat(), rid)
    menu_id = planning.save_week_as_menu(migrated_db, _MON, "M")
    recipes.delete_recipe(migrated_db, rid)
    assert planning.apply_menu_to_week(migrated_db, menu_id, date(2026, 7, 27)) == 0


def test_week_ical(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Pie")
    planning.add_recipe_entry(migrated_db, _MON.isoformat(), rid)
    ics = planning.week_ical(migrated_db, _MON, app_base_url="http://recipes.local")
    assert "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics
    assert "SUMMARY:Dinner: Pie" in ics
    assert "DTSTART;VALUE=DATE:20260720" in ics
    assert "http://recipes.local/recipes/" in ics


def test_board_route_and_shopping(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Weeknight Bowl")
    page = admin_client.get(f"/plan?start={_MON.isoformat()}")
    assert page.status_code == 200 and "Weeknight Bowl" in page.text  # in the add-recipe select
    add = admin_client.post(
        "/plan/entry",
        data={"week_start": _MON.isoformat(), "plan_date": _MON.isoformat(),
              "recipe_id": str(rid), "slot": "dinner", "servings": "4"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert add.status_code == 303
    assert len(planning.week_board(migrated_db, _MON)[0].entries) == 1
    ship = admin_client.post(
        "/plan/shopping", data={"week_start": _MON.isoformat()},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert ship.status_code == 303
    ics = admin_client.get(f"/plan/export.ics?start={_MON.isoformat()}")
    assert ics.status_code == 200 and "text/calendar" in ics.headers["content-type"]
