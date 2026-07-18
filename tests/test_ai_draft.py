"""AI-assisted manual entry: the draft helper (provider gating + logging) and /recipes/draft."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.ai.base import AIExtraction
from app.extraction import ExtractedIngredient, ExtractedRecipe, ExtractedStep
from app.services import ai_draft
from tests.conftest import SAME_ORIGIN

_DRAFT = ExtractedRecipe(
    title="Grandma's Chili",
    ingredients=[
        ExtractedIngredient(
            original_text="1 lb ground beef", quantity_text="1",
            unit="lb", food="ground beef",
        ),
    ],
    steps=[
        ExtractedStep(instruction="Brown the beef."),
        ExtractedStep(instruction="Simmer 90 min."),
    ],
)


class _FakeProvider:
    provider = "openai"
    model = "gpt-4o-mini"

    def __init__(self, recipe: ExtractedRecipe) -> None:
        self._recipe = recipe

    def extract(self, content: str, *, source_url: str) -> AIExtraction:
        raise AssertionError("the draft path must not call extract")

    def draft(self, description: str) -> AIExtraction:
        return AIExtraction(
            recipe=self._recipe, provider=self.provider, model=self.model,
            input_tokens=300, output_tokens=120, cost_micros=456,
        )


def test_draft_without_provider_reports_error(migrated_db: sqlite3.Connection) -> None:
    result = ai_draft.draft_from_description(migrated_db, "grandma's chili")
    assert result.recipe is None
    assert result.error is not None


def test_draft_empty_description(migrated_db: sqlite3.Connection) -> None:
    assert ai_draft.draft_from_description(migrated_db, "   ").recipe is None


def test_draft_success_logs_usage(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _FakeProvider(_DRAFT))
    result = ai_draft.draft_from_description(migrated_db, "grandma's chili, simmer 90 min")
    assert result.recipe is not None and result.recipe.title == "Grandma's Chili"
    row = migrated_db.execute(
        "SELECT operation, status, cost_micros FROM ai_usage_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["operation"] == "manual_draft"
    assert row["status"] == "ok"
    assert row["cost_micros"] == 456


def test_draft_route_prefills_form(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _FakeProvider(_DRAFT))
    resp = admin_client.post(
        "/recipes/draft", data={"describe": "grandma's chili, serves 6"}, headers=SAME_ORIGIN
    )
    assert resp.status_code == 200
    assert "Chili" in resp.text  # title pre-filled into the form
    assert "ground beef" in resp.text  # ingredient pre-filled
    assert "Drafted with AI" in resp.text


def test_draft_route_needs_a_description(admin_client: TestClient) -> None:
    resp = admin_client.post("/recipes/draft", data={"describe": ""}, headers=SAME_ORIGIN)
    assert resp.status_code == 400
    assert "Describe your recipe" in resp.text
