"""Per-recipe rating (1-5 stars) and the cook's notes: service clamping + the save routes."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import recipes
from tests.conftest import SAME_ORIGIN


def _make(conn: sqlite3.Connection) -> int:
    return recipes.create_recipe(conn, recipes.RecipeInput(title="Chili"))


def test_set_rating_clamps_and_clears(migrated_db: sqlite3.Connection) -> None:
    rid = _make(migrated_db)
    recipes.set_rating(migrated_db, rid, 4)
    got = recipes.get_recipe(migrated_db, rid)
    assert got is not None and got.rating == 4

    recipes.set_rating(migrated_db, rid, 9)  # out of range -> clamped to 5
    got = recipes.get_recipe(migrated_db, rid)
    assert got is not None and got.rating == 5

    recipes.set_rating(migrated_db, rid, 0)  # 0 clears the rating
    got = recipes.get_recipe(migrated_db, rid)
    assert got is not None and got.rating is None


def test_set_notes_sets_and_clears(migrated_db: sqlite3.Connection) -> None:
    rid = _make(migrated_db)
    recipes.set_notes(migrated_db, rid, "  double the garlic next time  ")
    got = recipes.get_recipe(migrated_db, rid)
    assert got is not None and got.notes == "double the garlic next time"

    recipes.set_notes(migrated_db, rid, "   ")  # blank clears
    got = recipes.get_recipe(migrated_db, rid)
    assert got is not None and got.notes is None


def _create_web(client: TestClient) -> None:
    client.post(
        "/recipes/new",
        data={
            "title": "Chili",
            "base_servings": "4",
            "steps": "Brown the beef.\nSimmer.",
            "ing_section": [""],
            "ing_qty": [""],
            "ing_unit": [""],
            "ing_food": [""],
            "ing_note": [""],
            "ing_scaling": ["linear"],
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def test_rating_route_marks_stars(admin_client: TestClient) -> None:
    _create_web(admin_client)
    resp = admin_client.post(
        "/recipes/chili/rating", data={"rating": "5"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert resp.status_code == 303
    view = admin_client.get("/recipes/chili").text
    assert view.count("star is-filled") == 5


def test_notes_route_saves_and_shows(admin_client: TestClient) -> None:
    _create_web(admin_client)
    admin_client.post(
        "/recipes/chili/notes",
        data={"notes": "needs more salt"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "needs more salt" in admin_client.get("/recipes/chili").text
