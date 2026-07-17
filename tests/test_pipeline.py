"""The ingestion pipeline end-to-end, offline via supplied HTML: queued job -> saved recipe,
failure categories, replay safety, and the deferred YouTube path."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.ai import usage as ai_usage
from app.ai.base import AIExtraction
from app.extraction import ExtractedIngredient, ExtractedRecipe, ExtractedStep
from app.services import ingest, pipeline, recipes

_FIXTURE = Path(__file__).parent / "fixtures" / "schema_org_recipe.html"

_AI_RECIPE = ExtractedRecipe(
    title="AI Recipe",
    ingredients=[ExtractedIngredient(original_text="1 egg")],
    steps=[ExtractedStep(instruction="Fry it.")],
)


class _FakeExtractor:
    provider = "anthropic"
    model = "claude-sonnet-5"

    def __init__(self, recipe: ExtractedRecipe) -> None:
        self._recipe = recipe

    def extract(self, content: str, *, source_url: str) -> AIExtraction:
        return AIExtraction(
            recipe=self._recipe, provider=self.provider, model=self.model,
            input_tokens=800, output_tokens=200, cost_micros=1234,
        )


def _enqueue_with_fixture(conn: sqlite3.Connection) -> ingest.IngestJob:
    html = _FIXTURE.read_text(encoding="utf-8")
    job, _ = ingest.enqueue_job(conn, "https://example.test/carrot-soup", html=html)
    return job


def test_run_job_creates_recipe_with_provenance(migrated_db: sqlite3.Connection) -> None:
    job = _enqueue_with_fixture(migrated_db)
    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None
    assert done.status == "done"
    assert done.recipe_id is not None

    recipe = recipes.get_recipe(migrated_db, done.recipe_id)
    assert recipe is not None
    assert recipe.title == "Cozy Carrot Soup"
    assert recipe.source_type == "web"
    assert recipe.total_minutes == 35
    assert len(recipe.ingredients) == 4
    assert len(recipe.steps) == 3

    run = migrated_db.execute(
        "SELECT extractor, schema_version, confidence FROM extraction_runs WHERE recipe_id = ?",
        (done.recipe_id,),
    ).fetchone()
    assert run["extractor"] == "recipe_scrapers"
    assert run["confidence"] == "high"

    prov = migrated_db.execute(
        "SELECT normalized_source_url, current_extraction_run_id FROM recipes WHERE id = ?",
        (done.recipe_id,),
    ).fetchone()
    assert prov["normalized_source_url"] == job.normalized_url
    assert prov["current_extraction_run_id"] is not None


def test_run_job_without_recipe_marks_failed(migrated_db: sqlite3.Connection) -> None:
    plain = "<html><head><title>Blog</title></head><body>no recipe here</body></html>"
    job, _ = ingest.enqueue_job(migrated_db, "https://example.test/blog", html=plain)
    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error_category == "no_recipe"
    assert done.recipe_id is None


def test_run_job_is_replay_safe(migrated_db: sqlite3.Connection) -> None:
    job = _enqueue_with_fixture(migrated_db)
    pipeline.run_job(migrated_db, job)
    # Replay with the STALE job (its recipe_id is still None) - the source-URL dedup in
    # apply_extraction must reuse the recipe rather than create a second one.
    pipeline.run_job(migrated_db, job)
    assert migrated_db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1


def test_youtube_job_is_deferred(migrated_db: sqlite3.Connection) -> None:
    job, _ = ingest.enqueue_job(migrated_db, "https://www.youtube.com/watch?v=abc123")
    pipeline.run_job(migrated_db, job)
    done = ingest.get_job(migrated_db, job.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error_category == "unsupported"


def test_pipeline_falls_back_to_ai(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = "<html><head><title>Blog</title></head><body>no recipe schema here</body></html>"
    job, _ = ingest.enqueue_job(migrated_db, "https://example.test/noschema", html=plain)
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _FakeExtractor(_AI_RECIPE))

    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None
    assert done.status == "done"
    assert done.recipe_id is not None
    recipe = recipes.get_recipe(migrated_db, done.recipe_id)
    assert recipe is not None and recipe.title == "AI Recipe"

    run = migrated_db.execute(
        "SELECT extractor, provider, confidence FROM extraction_runs WHERE recipe_id = ?",
        (done.recipe_id,),
    ).fetchone()
    assert run["extractor"] == "llm_web"
    assert run["provider"] == "anthropic"
    assert run["confidence"] == "medium"

    usage = migrated_db.execute(
        "SELECT status, cost_micros FROM ai_usage_log WHERE job_id = ?", (job.id,)
    ).fetchone()
    assert usage["status"] == "ok"
    assert usage["cost_micros"] == 1234


def test_pipeline_ai_blocked_by_budget(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = "<html><body>no recipe schema</body></html>"
    job, _ = ingest.enqueue_job(migrated_db, "https://example.test/noschema2", html=plain)
    # Exhaust the daily cap ($1.00 default = 1_000_000 micro-USD) before the job runs.
    ai_usage.log_usage(
        migrated_db, provider="anthropic", model="m", operation="extract_web",
        job_id=None, cost_micros=5_000_000, status="ok",
    )

    def _provider(_settings: Any) -> _FakeExtractor:
        return _FakeExtractor(_AI_RECIPE)

    monkeypatch.setattr("app.ai.get_provider", _provider)

    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error_category == "no_recipe"
    blocked = migrated_db.execute(
        "SELECT status FROM ai_usage_log WHERE job_id = ?", (job.id,)
    ).fetchone()
    assert blocked["status"] == "blocked"
