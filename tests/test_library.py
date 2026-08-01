"""Library browse: inbox / cookbook / archive views and FTS search."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import SAME_ORIGIN


def _create(admin_client: TestClient, title: str, food: str = "flour") -> str:
    resp = admin_client.post(
        "/recipes/new",
        data={
            "title": title,
            "base_servings": "4",
            "ing_section": [""],
            "ing_qty": ["1"],
            "ing_unit": ["cup"],
            "ing_food": [food],
            "ing_note": [""],
            "ing_scaling": ["linear"],
            "steps": "",
            "tags": "",
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    return resp.headers["location"].rsplit("/", 1)[-1]


def test_inbox_lists_and_searches(admin_client: TestClient) -> None:
    _create(admin_client, "Tomato Pasta", food="tomato")
    _create(admin_client, "Chicken Soup", food="chicken")

    inbox = admin_client.get("/inbox")
    assert "Tomato Pasta" in inbox.text
    assert "Chicken Soup" in inbox.text

    by_title = admin_client.get("/inbox", params={"q": "pasta"})
    assert "Tomato Pasta" in by_title.text
    assert "Chicken Soup" not in by_title.text

    by_ingredient = admin_client.get("/inbox", params={"q": "chicken"})
    assert "Chicken Soup" in by_ingredient.text
    assert "Tomato Pasta" not in by_ingredient.text


def test_archive_separates_from_inbox(admin_client: TestClient) -> None:
    slug = _create(admin_client, "Old Recipe")
    admin_client.post(
        f"/recipes/{slug}/status",
        data={"status": "archived"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "Old Recipe" not in admin_client.get("/inbox").text
    assert "Old Recipe" in admin_client.get("/archive").text


def test_search_no_match(admin_client: TestClient) -> None:
    _create(admin_client, "Tomato Pasta")
    resp = admin_client.get("/inbox", params={"q": "zzznothing"})
    assert "Tomato Pasta" not in resp.text
    # A no-match is an empty state that says what to do next, not a bare line of text.
    assert "empty-state" in resp.text and "Nothing matched that" in resp.text
