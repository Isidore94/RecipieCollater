"""Cook mode + after-cook over HTTP: the cook page, capture, promotion, cook log, and staleness."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import SAME_ORIGIN


def _create(client: TestClient) -> None:
    client.post(
        "/recipes/new",
        data={
            "title": "Stew",
            "base_servings": "4",
            "steps": "Brown the beef.\nSimmer 30 minutes.",
            "ing_section": [""],
            "ing_qty": ["2"],
            "ing_unit": ["cups"],
            "ing_food": ["broth"],
            "ing_note": [""],
            "ing_scaling": ["linear"],
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def test_cook_page_renders_steps_and_timer(admin_client: TestClient) -> None:
    _create(admin_client)
    resp = admin_client.get("/recipes/stew/cook")
    assert resp.status_code == 200
    assert "Brown the beef." in resp.text
    assert "Simmer 30 minutes." in resp.text
    assert "30 minutes" in resp.text  # timer parsed from the step
    assert "cook.js" in resp.text


def test_after_cook_records_promotes_and_logs(admin_client: TestClient) -> None:
    _create(admin_client)  # status defaults to inbox
    resp = admin_client.post(
        "/recipes/stew/after-cook",
        data={
            "rating": "9", "active_minutes": "20", "elapsed_minutes": "35",
            "servings_made": "4", "notes": "great stew", "promote": "on",
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303

    view = admin_client.get("/recipes/stew").text
    assert "Cook log" in view
    assert "great stew" in view
    assert "9/10" in view  # rating mirrored onto the recipe

    assert "Stew" in admin_client.get("/cookbook").text  # promoted out of the inbox


def test_after_cook_rejects_bad_servings(admin_client: TestClient) -> None:
    _create(admin_client)
    resp = admin_client.post(
        "/recipes/stew/after-cook", data={"servings_made": "0"}, headers=SAME_ORIGIN
    )
    assert resp.status_code == 400


def test_after_cook_rejects_non_numeric_servings(admin_client: TestClient) -> None:
    # A non-numeric amount must re-render 400, not 500 (QuantityError vs CookError).
    _create(admin_client)
    resp = admin_client.post(
        "/recipes/stew/after-cook", data={"servings_made": "two"}, headers=SAME_ORIGIN
    )
    assert resp.status_code == 400


def test_cookbook_staleness_sort(admin_client: TestClient) -> None:
    _create(admin_client)
    admin_client.post(
        "/recipes/stew/after-cook", data={"promote": "on", "rating": "5"}, headers=SAME_ORIGIN
    )
    resp = admin_client.get("/cookbook?sort=stale")
    assert resp.status_code == 200
    assert "Stew" in resp.text
    assert "Last cooked" in resp.text


def test_cook_requires_login(client: TestClient) -> None:
    resp = client.get("/recipes/anything/cook", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307, 401)
