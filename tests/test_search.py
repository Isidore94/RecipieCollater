"""Global search: the nav-shell box, the typeahead partial, and the grouped results page."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import pantry, recipes
from app.services.units import seed_core_units


def _world(conn: sqlite3.Connection) -> None:
    seed_core_units(conn)
    for title, tags, status in (
        ("Chicken Chili", ["dinner", "chicken"], "cookbook"),
        ("Chickpea Curry", ["dinner"], "inbox"),
        ("Chicken Pie", ["chicken"], "archived"),
        ("Pancakes", ["breakfast"], "cookbook"),
    ):
        rid = recipes.create_recipe(
            conn,
            recipes.RecipeInput(
                title=title,
                base_servings="4",
                tags=tags,
                ingredients=[
                    recipes.IngredientInput(quantity_text="1", unit="cup", food="chicken stock")
                ],
                steps=[recipes.StepInput(instruction="Cook.")],
            ),
        )
        recipes.set_status(conn, rid, status)
    loc = pantry.create_location(conn, "Fridge")
    pantry.add_item(
        conn,
        pantry.PantryItemInput(display_name="Chicken thighs", location_id=loc, food="chicken"),
    )


def test_nav_shell_has_search_box_for_both_layouts(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    assert 'id="global-search-rail"' in html and 'id="global-search-mobile"' in html
    assert 'action="/search"' in html and 'hx-trigger="keyup changed delay:250ms' in html


def test_results_are_grouped_and_exclude_archived(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _world(migrated_db)
    resp = admin_client.get("/search", params={"q": "chicken"})
    assert resp.status_code == 200
    html = resp.text
    for heading in ("Recipes", "Pantry items", "Foods", "Tags"):
        assert f">{heading}" in html
    assert "Chicken Chili" in html
    assert "Chicken Pie" not in html  # archived
    assert "Chicken thighs" in html  # pantry group
    assert "/foods?q=chicken" in html and "/pantry?q=chicken" in html
    assert "/cookbook?q=chicken" in html
    assert "/cookbook?tag=chicken" in html  # tag group links to the filtered cookbook


def test_results_keep_tag_filter(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    _world(migrated_db)
    both = admin_client.get("/search", params=[("q", "stock"), ("tag", "dinner")]).text
    assert "Chicken Chili" in both and "Pancakes" not in both
    html = admin_client.get("/search", params=[("q", "stock"), ("tag", "breakfast")]).text
    assert "Pancakes" in html and "Chicken Chili" not in html
    assert "breakfast &times;" in html  # removable chip


def test_empty_query_shows_recent_and_tags(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _world(migrated_db)
    resp = admin_client.get("/search")
    assert resp.status_code == 200
    assert "Recently in the Cookbook" in resp.text and "Pancakes" in resp.text
    assert "Try a tag" in resp.text
    assert admin_client.get("/search", params={"q": "   "}).status_code == 200


def test_suggest_partial(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    _world(migrated_db)
    resp = admin_client.get("/search/suggest", params={"q": "chic"})  # prefix match
    assert resp.status_code == 200
    assert "<html" not in resp.text  # a partial, not a page
    assert "Chicken Chili" in resp.text and "Chickpea Curry" in resp.text
    assert "Chicken Pie" not in resp.text
    assert "Food" in resp.text and "chicken stock" in resp.text
    assert "/search?q=chic" in resp.text
    assert admin_client.get("/search/suggest", params={"q": ""}).text.strip() == ""


def test_suggest_is_bounded(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    for n in range(9):
        recipes.create_recipe(
            migrated_db,
            recipes.RecipeInput(
                title=f"Soup number {n}",
                base_servings="2",
                ingredients=[],
                steps=[recipes.StepInput(instruction="Heat.")],
            ),
        )
    text = admin_client.get("/search/suggest", params={"q": "soup"}).text
    assert text.count("suggest-kind") == 5


def test_query_is_escaped(admin_client: TestClient) -> None:
    payload = '"><script>alert(1)</script>'
    for path in ("/search", "/search/suggest"):
        resp = admin_client.get(path, params={"q": payload})
        assert resp.status_code == 200
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text


def test_search_requires_login(client: TestClient) -> None:
    resp = client.get("/search", params={"q": "x"}, follow_redirects=False)
    assert resp.status_code in (303, 401)
    suggest = client.get("/search/suggest", params={"q": "x"}, follow_redirects=False)
    assert suggest.status_code in (303, 401)
