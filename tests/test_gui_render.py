"""Render smokes for the Phase 4.6 screens WITH data - Jinja errors only appear at render time."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import cooking, foods, pantry, recipes, shopping
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _setup_world(conn: sqlite3.Connection) -> str:
    """A cookbook recipe with rating/tags/coverage, pantry stock, a cook with deviations,
    and a shopping list with provenance + a checked line. Returns the recipe slug."""
    seed_core_units(conn)
    rid = recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title="Family Chili", base_servings="4", tier="family", tags=["dinner", "beef"],
            ingredients=[
                recipes.IngredientInput(quantity_text="500", unit="grams", food="ground beef"),
                recipes.IngredientInput(quantity_text="200", unit="grams", food="sour cream"),
            ],
            steps=[recipes.StepInput(instruction="Simmer for 20 minutes.")],
        ),
    )
    recipes.set_status(conn, rid, "cookbook")
    recipes.set_rating(conn, rid, 9)
    loc = pantry.create_location(conn, "Fridge")
    pantry.add_item(
        conn,
        pantry.PantryItemInput(display_name="Ground beef", location_id=loc, food="ground beef"),
    )
    detail = recipes.get_recipe(conn, rid)
    assert detail is not None
    cooking.record_cook(
        conn, rid,
        cooking.CookCaptureInput(
            rating=9,
            deviations={
                detail.ingredients[1].id: cooking.DeviationInput(
                    kind="substituted", text="greek yogurt"
                )
            },
            additions="a splash of beer",
        ),
    )
    lst = shopping.active_list(conn)
    shopping.add_from_recipe(conn, lst, rid)
    item = shopping.list_items(conn, lst)[0]
    shopping.toggle(conn, item.id)
    return detail.slug


def test_all_data_bearing_screens_render(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    slug = _setup_world(migrated_db)

    home = admin_client.get("/")
    assert home.status_code == 200 and "Family Chili" in home.text

    cookbook = admin_client.get("/cookbook")
    assert cookbook.status_code == 200 and "Family Chili" in cookbook.text

    can_make = admin_client.get("/can-make")
    assert can_make.status_code == 200

    view = admin_client.get(f"/recipes/{slug}")
    assert view.status_code == 200
    assert "greek yogurt" in view.text  # cook-log deviation rendered
    assert "remember this sub" in view.text
    assert "/cookbook?tag=dinner" in view.text  # tags are clickable

    assert admin_client.get(f"/recipes/{slug}/cook").status_code == 200
    assert admin_client.get(f"/recipes/{slug}/after-cook").status_code == 200

    shopping_page = admin_client.get("/shopping")
    assert shopping_page.status_code == 200

    restock = admin_client.get("/shopping/restock")
    assert restock.status_code == 200 and "Put it away" in restock.text

    pantry_page = admin_client.get("/pantry")
    assert pantry_page.status_code == 200


def test_purchase_prompt_roundtrip(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _setup_world(migrated_db)
    food_id = int(
        migrated_db.execute("SELECT id FROM foods WHERE name = 'sour cream'").fetchone()["id"]
    )
    resp = admin_client.post(
        f"/shopping/foods/{food_id}/purchase",
        data={"label": "tub", "quantity": "250", "unit": "g"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    info = foods.get_purchase(migrated_db, food_id)
    assert info is not None and info.label == "tub"


def test_food_name_cannot_break_out_of_merge_confirm(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    """Stored XSS guard: an ingested food named to escape a JS string must stay inert - the
    confirm() text is static and never interpolates the food name."""
    seed_core_units(migrated_db)
    recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Evil", base_servings="4",
            ingredients=[
                recipes.IngredientInput(
                    quantity_text="1", unit="each", food="x'); alert(1);//"
                )
            ],
        ),
    )
    page = admin_client.get("/foods")
    assert page.status_code == 200
    assert "Merge this food into the selected one" in page.text  # static confirm text
    assert "confirm('Merge x" not in page.text  # the name never enters the JS string


def test_inline_rename_roundtrip(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    slug = _setup_world(migrated_db)
    page = admin_client.get(f"/recipes/{slug}")
    assert f"/recipes/{slug}/rename" in page.text
    resp = admin_client.post(
        f"/recipes/{slug}/rename", data={"title": "Tuesday Chili"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Tuesday Chili" in admin_client.get(f"/recipes/{slug}").text


def test_phase5_6_screens_render(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    slug = _setup_world(migrated_db)
    assert admin_client.get("/plan").status_code == 200
    assert admin_client.get("/preferences").status_code == 200
    assert admin_client.get("/chat").status_code == 200
    assert admin_client.get("/admin/dashboard").status_code == 200
    # the new-recipe form carries the photo-import box
    assert "Read from photo" in admin_client.get("/recipes/new").text
    assert slug  # sanity
