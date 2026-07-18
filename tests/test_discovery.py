"""Home-screen discovery, "what can I make", cookbook filters, and the new GUI routes."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import discovery, matching, pantry, recipes
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _recipe(
    conn: sqlite3.Connection,
    title: str,
    foods_list: list[str],
    *,
    status: str = "cookbook",
    rating: int | None = None,
    tags: list[str] | None = None,
    tier: str | None = None,
) -> int:
    rid = recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=title, base_servings="4", tier=tier, tags=tags or [],
            ingredients=[
                recipes.IngredientInput(quantity_text="1", unit="each", food=f)
                for f in foods_list
            ],
        ),
    )
    if status != "inbox":
        recipes.set_status(conn, rid, status)
    if rating:
        recipes.set_rating(conn, rid, rating)
    return rid


def _stock(conn: sqlite3.Connection, food: str) -> int:
    loc = pantry.create_location(conn, "Pantry")
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(display_name=food.title(), location_id=loc, food=food),
    )


def test_batch_coverage_matches_per_recipe(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    ready = _recipe(migrated_db, "Ready", ["eggs"])
    short = _recipe(migrated_db, "Short", ["eggs", "saffron"])
    _stock(migrated_db, "eggs")
    coverage = matching.batch_coverage(migrated_db)
    assert coverage[ready].complete is True
    assert (coverage[short].have, coverage[short].total) == (1, 2)
    assert coverage[short].missing_names == ["saffron"]


def test_can_make_groups_by_shortfall(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "Ready", ["eggs"])
    _recipe(migrated_db, "One short", ["eggs", "saffron"])
    _recipe(migrated_db, "Two short", ["eggs", "saffron", "truffle"])
    _stock(migrated_db, "eggs")
    groups = discovery.can_make(migrated_db)
    assert [e.summary.title for e in groups.ready] == ["Ready"]
    assert [e.summary.title for e in groups.missing_one] == ["One short"]
    assert [e.summary.title for e in groups.missing_two] == ["Two short"]


def test_tonight_picks_prefers_rested_favourites(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    from app.services import cooking

    loved = _recipe(migrated_db, "Loved + rested", ["eggs"], rating=9)
    _recipe(migrated_db, "Unrated", ["milk"])
    fresh = _recipe(migrated_db, "Cooked yesterday", ["rice"], rating=10)
    cooking.record_cook(migrated_db, fresh, cooking.CookCaptureInput())  # cooked_at = now

    picks = discovery.tonight_picks(migrated_db)
    titles = [p.title for p in picks]
    assert titles[0] == "Loved + rested"  # highest rated among rested
    assert "Cooked yesterday" not in titles  # hasn't rested yet
    assert recipes.get_recipe(migrated_db, loved) is not None


def test_missing_ingredient_suggests_learned_and_family_subs(
    migrated_db: sqlite3.Connection,
) -> None:
    from app.services import foods as foods_service

    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Bake", ["buttermilk"], status="cookbook")
    _stock(migrated_db, "milk")
    buttermilk = next(
        int(r["id"]) for r in migrated_db.execute(
            "SELECT id FROM foods WHERE name = 'buttermilk'"
        ).fetchall()
    )
    foods_service.record_substitute(migrated_db, buttermilk, "milk")
    cov = matching.recipe_coverage(migrated_db, rid)
    assert cov.missing[0].name == "buttermilk"
    subs = cov.missing[0].subs
    assert subs and subs[0].text == "milk" and subs[0].have is True


def test_home_and_can_make_routes(admin_client: TestClient) -> None:
    r = admin_client.get("/")
    assert r.status_code == 200 and "What can I make?" in r.text
    r = admin_client.get("/can-make")
    assert r.status_code == 200


def test_cookbook_filters(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "Weeknight Pasta", ["pasta"], tags=["italian", "weeknight"], rating=9)
    _recipe(migrated_db, "Project Brisket", ["beef"], tags=["project"], rating=7, tier="company")

    listed = recipes.list_recipes(migrated_db, status="cookbook", tag="italian")
    assert [r.title for r in listed] == ["Weeknight Pasta"]
    listed = recipes.list_recipes(migrated_db, status="cookbook", min_rating=8)
    assert [r.title for r in listed] == ["Weeknight Pasta"]
    listed = recipes.list_recipes(migrated_db, status="cookbook", tier="company")
    assert [r.title for r in listed] == ["Project Brisket"]
    tags = {t.name for t in recipes.list_tags(migrated_db, status="cookbook")}
    assert {"italian", "weeknight", "project"} <= tags

    resp = admin_client.get("/cookbook?tag=italian")
    assert resp.status_code == 200
    assert "Weeknight Pasta" in resp.text and "Project Brisket" not in resp.text


def test_foods_screen_routes(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "Bread", ["flour"], status="inbox")
    assert admin_client.get("/foods").status_code == 200
    food_id = int(
        migrated_db.execute("SELECT id FROM foods WHERE name = 'flour'").fetchone()["id"]
    )
    resp = admin_client.post(
        f"/foods/{food_id}/details",
        data={"category": "Baking", "purchase_label": "bag", "purchase_quantity": "2",
              "purchase_unit": "kg"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    row = migrated_db.execute(
        "SELECT category, purchase_label FROM foods WHERE id = ?", (food_id,)
    ).fetchone()
    assert row["category"] == "Baking" and row["purchase_label"] == "bag"


def test_trip_and_restock_routes(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Omelette", ["eggs"], status="cookbook")
    assert admin_client.get("/shopping/plan").status_code == 200
    resp = admin_client.post(
        "/shopping/plan/preview", data={"recipe": str(rid)},
        headers=SAME_ORIGIN,
    )
    assert resp.status_code == 200 and "eggs" in resp.text
    eggs_id = int(
        migrated_db.execute("SELECT id FROM foods WHERE name = 'eggs'").fetchone()["id"]
    )
    resp = admin_client.post(
        "/shopping/plan/apply", data={"recipe": str(rid), "line": f"f:{eggs_id}"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    # check something off, then the restock review renders
    listing = admin_client.get("/shopping")
    assert listing.status_code == 200
