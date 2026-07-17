"""Recipe sheet: serving scaler (view + fragment) and JSON / Markdown export."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import SAME_ORIGIN


def _make(admin_client: TestClient) -> None:
    admin_client.post(
        "/recipes/new",
        data={
            "title": "Tomato Pasta",
            "base_servings": "4",
            "ing_section": [""],
            "ing_qty": ["2"],
            "ing_unit": ["cups"],
            "ing_food": ["flour"],
            "ing_note": [""],
            "ing_scaling": ["linear"],
            "steps": "Mix.\nCook.",
            "tags": "italian",
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def test_view_scaled_via_query(admin_client: TestClient) -> None:
    _make(admin_client)
    base = admin_client.get("/recipes/tomato-pasta")
    assert "2 cups flour" in base.text
    scaled = admin_client.get("/recipes/tomato-pasta?servings=6")
    assert "3 cups flour" in scaled.text
    assert "scaled to 6" in scaled.text


def test_ingredients_fragment(admin_client: TestClient) -> None:
    _make(admin_client)
    frag = admin_client.get("/recipes/tomato-pasta/ingredients?servings=8")
    assert frag.status_code == 200
    assert "4 cups flour" in frag.text  # 2 * (8/4)
    assert "<html" not in frag.text.lower()  # a partial, not the whole page


def test_bad_servings_falls_back_to_base(admin_client: TestClient) -> None:
    _make(admin_client)
    resp = admin_client.get("/recipes/tomato-pasta?servings=not-a-number")
    assert resp.status_code == 200
    assert "2 cups flour" in resp.text


def test_export_markdown(admin_client: TestClient) -> None:
    _make(admin_client)
    resp = admin_client.get("/recipes/tomato-pasta/export.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Tomato Pasta" in resp.text
    assert "- 2 cups flour" in resp.text
    assert "1. Mix." in resp.text


def test_export_json(admin_client: TestClient) -> None:
    _make(admin_client)
    resp = admin_client.get("/recipes/tomato-pasta/export.json")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["title"] == "Tomato Pasta"
    assert payload["ingredients"][0]["food_name"] == "flour"


def test_export_missing_is_404(admin_client: TestClient) -> None:
    assert admin_client.get("/recipes/nope/export.json").status_code == 404
    assert admin_client.get("/recipes/nope/export.md").status_code == 404
