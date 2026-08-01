"""Phase 6: admin dashboard, cookbook export, recipe photo import."""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.base import AIExtraction
from app.config import get_settings
from app.extraction import ExtractedIngredient, ExtractedRecipe, ExtractedStep
from app.services import admin_stats, cookbook_export, recipes
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _jpeg() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (50, 70), (245, 240, 235)).save(out, format="JPEG")
    return out.getvalue()


def _recipe(conn: sqlite3.Connection, title: str) -> int:
    return recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=title, base_servings="4", tags=["dinner"],
            ingredients=[recipes.IngredientInput(quantity_text="1", unit="each", food="egg")],
            steps=[recipes.StepInput(instruction="Cook it.")],
        ),
    )


# ---- admin dashboard -------------------------------------------------------------------


def test_dashboard_gather(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, "Eggs")
    recipes.set_status(migrated_db, rid, "cookbook")
    from app.ai import usage as ai_usage
    ai_usage.log_usage(
        migrated_db, provider="openai", model="gpt-4o-mini", operation="assist",
        job_id=None, input_tokens=100, output_tokens=50, cost_micros=1234, status="ok",
    )
    stats = admin_stats.gather(migrated_db, get_settings())
    assert stats.recipe_count == 1 and stats.cookbook_count == 1
    assert stats.schema_version == 17
    assert stats.spend_month_micros == 1234
    assert any(p.provider == "openai" for p in stats.provider_spend)
    assert stats.db_bytes > 0


def test_dashboard_route(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    resp = admin_client.get("/admin/dashboard")
    assert resp.status_code == 200 and "Dashboard" in resp.text


# ---- cookbook export -------------------------------------------------------------------


def test_export_writes_json_and_markdown(
    migrated_db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "Cozy Soup")
    count = cookbook_export.export_all(migrated_db, tmp_path / "cb")
    assert count == 1
    md = list((tmp_path / "cb" / "markdown").glob("*.md"))
    js = list((tmp_path / "cb" / "json").glob("*.json"))
    assert len(md) == 1 and len(js) == 1
    assert "# Cozy Soup" in md[0].read_text(encoding="utf-8")
    assert (tmp_path / "cb" / "index.json").is_file()


def test_export_command(migrated_db: sqlite3.Connection, tmp_path: Path) -> None:
    from app.manage import main

    seed_core_units(migrated_db)
    _recipe(migrated_db, "X")
    assert main(["export-cookbook", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "index.json").is_file()


# ---- recipe photo import ---------------------------------------------------------------


@dataclass
class _PhotoProvider:
    provider: str = "fake"
    model: str = "fake-1"
    got_image: bool = False

    def recipe_from_photo(self, image_jpeg: bytes) -> AIExtraction:
        self.got_image = True
        recipe = ExtractedRecipe(
            title="Grandma's Chili",
            ingredients=[ExtractedIngredient(original_text="1 lb ground beef")],
            steps=[ExtractedStep(instruction="Brown the beef.")],
        )
        return AIExtraction(
            recipe=recipe, provider=self.provider, model=self.model,
            input_tokens=200, output_tokens=100, cost_micros=50,
        )

    def extract(self, content: str, *, source_url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def draft(self, description: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def receipt(self, content: str, *, image_jpeg: bytes | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def assist(self, content: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def test_photo_draft_prefills_form(
    admin_client: TestClient, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _PhotoProvider()
    monkeypatch.setattr("app.ai.get_provider", lambda settings: provider)
    resp = admin_client.post(
        "/recipes/photo-draft",
        files={"photo": ("card.jpg", _jpeg(), "image/jpeg")},
        headers=SAME_ORIGIN,
    )
    assert resp.status_code == 200
    assert provider.got_image is True
    assert "Grandma&#39;s Chili" in resp.text or "Grandma's Chili" in resp.text  # prefilled title
    # nothing was created - it's a draft in the form
    assert recipes.list_recipes(migrated_db) == []


def test_photo_draft_without_photo_is_gentle(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    resp = admin_client.post("/recipes/photo-draft", data={}, headers=SAME_ORIGIN)
    assert resp.status_code == 400 and "Choose a photo" in resp.text
