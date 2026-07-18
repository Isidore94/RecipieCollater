"""Shopping list over HTTP: view, manual add, add-from-recipe, toggle, export, auth gate."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import pantry, recipes, shopping
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def test_shopping_page_and_manual_add(admin_client: TestClient) -> None:
    assert admin_client.get("/shopping").status_code == 200
    resp = admin_client.post(
        "/shopping/add", data={"text": "paper towels"}, headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = admin_client.get("/shopping").text
    assert "paper towels" in page
    # the Copy-for-Reminders affordance is present with the item in its textarea
    assert "Copy for Reminders" in page and "shopping.js" in page


def test_add_from_recipe_route(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Bread", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="200", unit="grams", food="flour")],
        ),
    )
    detail = recipes.get_recipe(migrated_db, rid)
    assert detail is not None
    resp = admin_client.post(
        f"/shopping/from-recipe/{detail.slug}", data={"servings": "4"}, headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    lst = shopping.active_list(migrated_db)
    assert any(i.food_id is not None for i in shopping.list_items(migrated_db, lst))


def test_toggle_and_export(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    lst = shopping.active_list(migrated_db)
    item = shopping.add_manual(migrated_db, lst, "milk")
    admin_client.post(f"/shopping/items/{item}/toggle", headers=SAME_ORIGIN)
    assert shopping.list_items(migrated_db, lst)[0].checked is True

    resp = admin_client.get("/shopping/export.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_add_staples_route(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    rice = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="gauge", food="rice"
        ),
    )
    pantry.set_staple(migrated_db, rice, is_staple=True)
    pantry.set_gauge(migrated_db, rice, "out")
    admin_client.post("/shopping/add-staples", headers=SAME_ORIGIN)
    items = shopping.list_items(migrated_db, shopping.active_list(migrated_db))
    assert any(i.display_text == "Rice" for i in items)


def test_shopping_requires_login(client: TestClient) -> None:
    resp = client.get("/shopping", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307, 401)
