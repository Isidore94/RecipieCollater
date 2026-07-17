"""Recipe screens: create/view/edit/promote/delete over HTTP, auth, and validation."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from tests.conftest import SAME_ORIGIN


def _create(client: TestClient, **overrides: str | list[str]) -> httpx.Response:
    data: dict[str, str | list[str]] = {
        "title": "Tomato Pasta",
        "tldr": "Simmer and reduce.",
        "tier": "family",
        "base_servings": "4",
        "ing_section": [""],
        "ing_qty": ["2"],
        "ing_unit": ["cups"],
        "ing_food": ["flour"],
        "ing_note": [""],
        "ing_scaling": ["linear"],
        "steps": "Mix.\nCook.",
        "tags": "italian, weeknight",
    }
    data.update(overrides)
    return client.post("/recipes/new", data=data, headers=SAME_ORIGIN, follow_redirects=False)


def test_new_form_renders(admin_client: TestClient) -> None:
    resp = admin_client.get("/recipes/new")
    assert resp.status_code == 200
    assert "New recipe" in resp.text


def test_create_view_and_inbox(admin_client: TestClient) -> None:
    resp = _create(admin_client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/recipes/tomato-pasta"

    view = admin_client.get("/recipes/tomato-pasta")
    assert view.status_code == 200
    assert "Tomato Pasta" in view.text
    assert "flour" in view.text

    inbox = admin_client.get("/inbox")
    assert "Tomato Pasta" in inbox.text


def test_edit_updates(admin_client: TestClient) -> None:
    _create(admin_client)
    resp = admin_client.post(
        "/recipes/tomato-pasta/edit",
        data={
            "title": "Arrabbiata",
            "base_servings": "4",
            "steps": "",
            "tags": "spicy",
            "ing_qty": [""],
            "ing_unit": [""],
            "ing_food": [""],
            "ing_section": [""],
            "ing_note": [""],
            "ing_scaling": ["linear"],
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert admin_client.get("/recipes/tomato-pasta").text.count("Arrabbiata") >= 1


def test_promote_then_delete(admin_client: TestClient) -> None:
    _create(admin_client)
    promote = admin_client.post(
        "/recipes/tomato-pasta/status",
        data={"status": "cookbook"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert promote.status_code == 303
    assert "Tomato Pasta" in admin_client.get("/cookbook").text

    deleted = admin_client.post(
        "/recipes/tomato-pasta/delete", headers=SAME_ORIGIN, follow_redirects=False
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/inbox"
    # The recipe is gone: viewing it redirects back to the inbox.
    gone = admin_client.get("/recipes/tomato-pasta", follow_redirects=False)
    assert gone.status_code == 303


def test_validation_error_rerenders_form(admin_client: TestClient) -> None:
    resp = _create(admin_client, title="")
    assert resp.status_code == 400
    assert "title" in resp.text.lower()


def test_recipes_require_auth(client: TestClient) -> None:
    resp = client.get("/recipes/new", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/welcome"
