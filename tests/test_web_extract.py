"""schema.org / JSON-LD extraction (the ingestion fast-path) on a fixture - fully offline."""

from __future__ import annotations

from pathlib import Path

from app.services import web_extract

_FIXTURE = Path(__file__).parent / "fixtures" / "schema_org_recipe.html"


def test_extracts_recipe_from_json_ld() -> None:
    html = _FIXTURE.read_text(encoding="utf-8")
    recipe = web_extract.extract_from_html(html, "https://example.test/carrot-soup")
    assert recipe is not None
    assert recipe.title == "Cozy Carrot Soup"
    assert recipe.description == "A warming weeknight carrot soup."
    assert recipe.servings_text == "4 servings"
    assert recipe.total_minutes == 35
    assert recipe.prep_minutes == 10
    assert recipe.cook_minutes == 25
    assert [i.original_text for i in recipe.ingredients] == [
        "2 tablespoons olive oil",
        "1 pound carrots, peeled and chopped",
        "4 cups vegetable broth",
        "1 teaspoon salt",
    ]
    assert len(recipe.steps) == 3
    assert recipe.steps[0].instruction.startswith("Warm the olive oil")
    assert recipe.is_complete() is True


def test_page_without_recipe_schema_returns_none() -> None:
    plain = "<!DOCTYPE html><html><head><title>Blog</title></head><body><p>hi</p></body></html>"
    assert web_extract.extract_from_html(plain, "https://example.test/blog") is None
