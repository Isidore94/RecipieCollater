"""Pantry-aware matching: ingredient coverage and the 'use it up' ranking."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi.testclient import TestClient

from app.security import now
from app.services import matching, pantry, recipes
from app.services.units import seed_core_units


def _recipe(conn: sqlite3.Connection, foods: list[str]) -> int:
    return recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title="Dish", base_servings="4",
            ingredients=[
                recipes.IngredientInput(quantity_text="1", unit="each", food=f) for f in foods
            ],
        ),
    )


def _slug(conn: sqlite3.Connection, recipe_id: int) -> str:
    detail = recipes.get_recipe(conn, recipe_id)
    assert detail is not None
    return detail.slug


def _have(conn: sqlite3.Connection, food: str, *, expires: str | None = None) -> int:
    loc = pantry.create_location(conn, "Pantry")
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(
            display_name=food.title(), location_id=loc, quantity_mode="binary",
            food=food, expires_on=expires,
        ),
    )


def test_coverage_counts_have_and_missing(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, ["eggs", "milk", "flour"])
    _have(migrated_db, "eggs")
    _have(migrated_db, "milk")  # flour not stocked

    cov = matching.recipe_coverage(migrated_db, rid)
    assert (cov.have, cov.total) == (2, 3)
    assert cov.missing_names == ["flour"]
    assert cov.complete is False


def test_coverage_excludes_to_taste_and_unquantified(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Soup", base_servings="4",
            ingredients=[
                recipes.IngredientInput(quantity_text="2", unit="each", food="eggs"),
                recipes.IngredientInput(
                    original_text="salt to taste", food="salt", scaling_mode="to_taste"
                ),
                recipes.IngredientInput(original_text="a handful of basil"),  # no food/amount
            ],
        ),
    )
    _have(migrated_db, "eggs")
    cov = matching.recipe_coverage(migrated_db, rid)
    assert (cov.have, cov.total) == (1, 1)  # only the eggs count
    assert cov.complete is True


def test_binary_out_is_not_on_hand(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, ["eggs"])
    item = _have(migrated_db, "eggs")
    pantry.set_have(migrated_db, item, False)  # out
    cov = matching.recipe_coverage(migrated_db, rid)
    assert cov.have == 0


def test_use_it_up_ranks_by_expiring_items(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    soon = (now() + timedelta(days=3)).date().isoformat()
    later = (now() + timedelta(days=90)).date().isoformat()
    # spinach + cream expire soon; rice does not
    _have(migrated_db, "spinach", expires=soon)
    _have(migrated_db, "cream", expires=soon)
    _have(migrated_db, "rice", expires=later)

    both = _recipe(migrated_db, ["spinach", "cream", "rice"])  # uses 2 expiring
    one = _recipe(migrated_db, ["spinach", "rice"])  # uses 1 expiring
    recipes.set_status(migrated_db, both, "cookbook")
    recipes.set_status(migrated_db, one, "cookbook")

    ranking = matching.use_it_up(migrated_db)
    slugs = [u.slug for u in ranking]
    both_slug = _slug(migrated_db, both)
    one_slug = _slug(migrated_db, one)
    assert slugs.index(both_slug) < slugs.index(one_slug)  # 2-expiring recipe ranks first
    assert ranking[0].uses == 2


def test_use_it_up_empty_when_nothing_expiring(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, ["rice"])
    recipes.set_status(migrated_db, rid, "cookbook")
    _have(migrated_db, "rice")  # no expiry set
    assert matching.use_it_up(migrated_db) == []


def test_recipe_view_shows_coverage(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, ["eggs", "milk"])
    _have(migrated_db, "eggs")
    slug = _slug(migrated_db, rid)
    assert "of 2 ingredients" in admin_client.get(f"/recipes/{slug}").text


def test_cookbook_useitup_sort_renders(admin_client: TestClient) -> None:
    resp = admin_client.get("/cookbook?sort=useitup")
    assert resp.status_code == 200
    assert "Use it up" in resp.text or "expiring soon" in resp.text
