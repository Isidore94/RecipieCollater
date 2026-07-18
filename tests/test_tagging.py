"""AI tag backfill: offline, with a fake provider (CONVENTIONS 15 - no live calls)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from app.ai.base import DRAFT_SYSTEM, EXTRACT_SYSTEM, AIExtraction
from app.extraction import ExtractedRecipe
from app.services import recipes, tagging
from app.services.units import seed_core_units


@dataclass
class _FakeProvider:
    provider: str = "fake"
    model: str = "fake-1"
    tags: tuple[str, ...] = ("Dinner", "dinner", "chicken", "mexican", "weeknight", "a", "b", "c")

    def extract(self, content: str, *, source_url: str) -> AIExtraction:
        return AIExtraction(
            recipe=ExtractedRecipe(title="x", tags=list(self.tags)),
            provider=self.provider, model=self.model,
            input_tokens=10, output_tokens=5, cost_micros=1,
        )

    def draft(self, description: str) -> AIExtraction:  # pragma: no cover - unused here
        return self.extract(description, source_url="draft://")


def _recipe(conn: sqlite3.Connection, title: str, tags: list[str] | None = None) -> int:
    return recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=title, base_servings="4", tags=tags or [],
            ingredients=[
                recipes.IngredientInput(quantity_text="1", unit="each", food="chicken")
            ],
        ),
    )


def test_prompts_carry_the_tag_vocabulary() -> None:
    for prompt in (EXTRACT_SYSTEM, DRAFT_SYSTEM):
        assert "protein: chicken" in prompt
        assert "weeknight" in prompt


def test_backfill_tags_untagged_only_and_normalizes(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    bare = _recipe(migrated_db, "Bare")
    tagged = _recipe(migrated_db, "Tagged", tags=["kept"])
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _FakeProvider())

    results = tagging.backfill(migrated_db)

    assert [r.recipe_id for r in results] == [bare]  # already-tagged recipes untouched
    detail = recipes.get_recipe(migrated_db, bare)
    assert detail is not None
    assert len(detail.tags) <= 6  # capped
    assert "dinner" in detail.tags and "chicken" in detail.tags  # lowercased + deduped
    tagged_detail = recipes.get_recipe(migrated_db, tagged)
    assert tagged_detail is not None and tagged_detail.tags == ("kept",)
    # spend was logged
    row = migrated_db.execute(
        "SELECT COUNT(*) AS n FROM ai_usage_log WHERE operation = 'tag_backfill'"
    ).fetchone()
    assert row["n"] == 1


def test_backfill_without_provider_reports_error(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    _recipe(migrated_db, "Bare")
    monkeypatch.setattr("app.ai.get_provider", lambda settings: None)
    results = tagging.backfill(migrated_db)
    assert len(results) == 1 and results[0].error == "no AI provider configured"
