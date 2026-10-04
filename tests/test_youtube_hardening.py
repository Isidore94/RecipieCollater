"""YouTube hardening: bot-block classification, long backoff, caption-only review nudge. Offline."""

from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config
from app.ai.base import AIExtraction
from app.extraction import ExtractedIngredient, ExtractedRecipe, ExtractedStep
from app.services import ingest, pipeline, recipes, youtube

_RECIPE = ExtractedRecipe(
    title="Spoken Soup",
    ingredients=[ExtractedIngredient(original_text="1 onion")],
    steps=[ExtractedStep(instruction="Simmer.")],
)
_URL = "https://www.youtube.com/watch?v=blk123"
_LONG_DESC = "Ingredients:\n" + "\n".join(f"{n} cups of thing {n}" for n in range(1, 12))


@pytest.fixture(autouse=True)
def _no_image_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.images.store_image_from_url", lambda recipe_id, url: None)


class _Extractor:
    provider = "anthropic"
    model = "claude-sonnet-5"

    def extract(self, content: str, *, source_url: str) -> AIExtraction:
        return AIExtraction(
            recipe=_RECIPE, provider=self.provider, model=self.model,
            input_tokens=1, output_tokens=1, cost_micros=1,
        )


def _data(description: str, captions: str | None) -> youtube.YoutubeData:
    return youtube.YoutubeData(
        video_id="blk123", title="Soup", description=description, uploader="Chef",
        thumbnail_url=None, duration_seconds=60, captions=captions,
    )


def _enable_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RC_ANTHROPIC_API_KEY", "test-key")
    config.reset_settings_cache()
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _Extractor())


# ---- classifier ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies", "blocked"),
        ("Sign in to confirm you\u2019re not a bot", "blocked"),
        ("HTTP Error 429: Too Many Requests", "blocked"),
        ("This content isn\u2019t available, try again later.", "blocked"),
        ("The current session has been rate-limited by YouTube", "blocked"),
        ("This content isn't available", "blocked"),
        ("Video unavailable. This video has been removed by the uploader", "unavailable"),
        ("This video is private. Sign in if you've been granted access", "unavailable"),
        ("Unable to extract uploader id", "other"),
        ("", "other"),
    ],
)
def test_classify_error(message: str, kind: str) -> None:
    assert youtube.classify_error(message) == kind


def test_youtube_error_carries_kind() -> None:
    assert youtube.YoutubeError("x").kind == "other"
    assert youtube.YoutubeError("x", kind="blocked").kind == "blocked"


# ---- thin description ----------------------------------------------------------------------


def test_description_is_thin() -> None:
    assert youtube.description_is_thin("")
    assert youtube.description_is_thin("Subscribe! #cooking https://x.test/" + "a" * 300)
    assert youtube.description_is_thin("Great soup.\n" * 40)  # long but no ingredient lines
    assert not youtube.description_is_thin(_LONG_DESC)


def test_source_basis_and_json() -> None:
    assert _data("short", "we chop an onion").source_basis == "captions"
    assert _data("short", None).source_basis == "description"
    assert _data(_LONG_DESC, "spoken").source_basis == "description"
    assert json.loads(_data("short", "spoken").to_json())["source_basis"] == "captions"


# ---- backoff plan --------------------------------------------------------------------------


def test_plan_blocked_retry_schedule() -> None:
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    plans = [ingest.plan_blocked_retry(n, now) for n in range(4)]
    assert [p.delay_seconds if p else None for p in plans] == [1800, 7200, 21600, None]
    first = plans[0]
    assert first is not None and first.attempt == 1
    assert first.retry_at == now + timedelta(minutes=30)
    assert ingest.plan_blocked_retry(-1, now) is None


def test_waiting_message_names_time_and_retry(migrated_db: sqlite3.Connection) -> None:
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    plan = ingest.plan_blocked_retry(0, datetime.now(UTC))
    assert plan is not None
    ingest.mark_waiting_retry(migrated_db, job.id, plan)
    waiting = ingest.get_job(migrated_db, job.id)
    assert waiting is not None and ingest.is_waiting_retry(waiting)
    assert waiting.error_message is not None
    assert "Waiting to retry" in waiting.error_message and "retry 1 of 3" in waiting.error_message


# ---- pipeline ------------------------------------------------------------------------------


def _fail_with(monkeypatch: pytest.MonkeyPatch, exc: youtube.YoutubeError) -> None:
    def boom(url: str) -> youtube.YoutubeData:
        raise exc

    monkeypatch.setattr("app.services.youtube.fetch", boom)


def test_pipeline_blocked_category(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_ai(monkeypatch)
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    _fail_with(monkeypatch, youtube.YoutubeError("bot", kind="blocked"))
    pipeline.run_job(migrated_db, job)
    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.status == "failed"
    assert done.error_category == "youtube_blocked"
    assert done.error_message == ingest.YOUTUBE_BLOCKED_MESSAGE
    assert "clears on its own" in done.error_message
    # the manual Retry button still works for this category
    assert ingest.requeue_failed(migrated_db, job.id)


def test_pipeline_unavailable_and_other_categories(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_ai(monkeypatch)
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    _fail_with(monkeypatch, youtube.YoutubeError("gone", kind="unavailable"))
    pipeline.run_job(migrated_db, job)
    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.error_category == "youtube_unavailable"
    _fail_with(monkeypatch, youtube.YoutubeError("weird"))
    pipeline.run_job(migrated_db, done)
    again = ingest.get_job(migrated_db, job.id)
    assert again is not None and again.error_category == "youtube_fetch"
    assert again.error_message == "weird"


def _confidence(conn: sqlite3.Connection, job_id: int) -> tuple[str, int]:
    job = ingest.get_job(conn, job_id)
    assert job is not None and job.status == "done" and job.recipe_id is not None
    row = conn.execute(
        "SELECT confidence FROM extraction_runs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return row["confidence"], job.recipe_id


def test_captions_only_extraction_is_thin_confidence(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_ai(monkeypatch)
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    monkeypatch.setattr(
        "app.services.youtube.fetch", lambda url: _data("subscribe", "add an onion and simmer")
    )
    pipeline.run_job(migrated_db, job)
    confidence, recipe_id = _confidence(migrated_db, job.id)
    assert confidence == "thin"
    assert recipes.needs_transcript_review(migrated_db, recipe_id)
    stored = ingest.read_artifact(migrated_db, job.id, "youtube_metadata")
    assert stored is not None and json.loads(stored)["source_basis"] == "captions"


def test_description_extraction_stays_medium(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_ai(monkeypatch)
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    monkeypatch.setattr("app.services.youtube.fetch", lambda url: _data(_LONG_DESC, "spoken"))
    pipeline.run_job(migrated_db, job)
    confidence, recipe_id = _confidence(migrated_db, job.id)
    assert confidence == "medium"
    assert not recipes.needs_transcript_review(migrated_db, recipe_id)


def test_review_note_renders_on_sheet_and_inbox(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import connect

    _enable_ai(monkeypatch)
    conn = connect(config.get_settings().db_path)
    try:
        job, _ = ingest.enqueue_job(conn, _URL)
        monkeypatch.setattr(
            "app.services.youtube.fetch", lambda url: _data("subscribe", "spoken words")
        )
        pipeline.run_job(conn, job)
        done = ingest.get_job(conn, job.id)
        assert done is not None and done.recipe_id is not None
        recipe = recipes.get_recipe(conn, done.recipe_id)
        assert recipe is not None
    finally:
        conn.close()
    assert recipes.TRANSCRIPT_REVIEW_NOTE in admin_client.get(f"/recipes/{recipe.slug}").text
    assert recipes.TRANSCRIPT_REVIEW_NOTE in admin_client.get("/inbox/jobs").text


def test_waiting_job_shows_in_inbox(admin_client: TestClient) -> None:
    from app.db import connect

    conn = connect(config.get_settings().db_path)
    try:
        job, _ = ingest.enqueue_job(conn, _URL)
        plan = ingest.plan_blocked_retry(0, datetime.now(UTC))
        assert plan is not None
        ingest.mark_waiting_retry(conn, job.id, plan)
    finally:
        conn.close()
    frag = admin_client.get("/inbox/jobs").text
    assert "Waiting to retry automatically" in frag
    assert 'hx-trigger="every 3s"' in frag


# ---- worker task: bounded re-scheduling ----------------------------------------------------


@pytest.fixture
def tasks_module(migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("RC_HUEY_IMMEDIATE", "1")
    monkeypatch.setenv("RC_LOG_CONSOLE", "1")
    config.reset_settings_cache()
    import app.tasks as tasks_module

    return importlib.reload(tasks_module)


def test_task_reschedules_blocked_job_with_backoff(
    migrated_db: sqlite3.Connection, tasks_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    scheduled: list[tuple[tuple[int, int], int]] = []
    monkeypatch.setattr(
        tasks_module.process_ingest_job, "schedule",
        lambda args, delay: scheduled.append((args, delay)),
    )

    def blocked_run(conn: sqlite3.Connection, j: ingest.IngestJob) -> None:
        ingest.set_status(
            conn, j.id, "failed", error_category="youtube_blocked", error_message="blocked"
        )

    monkeypatch.setattr("app.services.pipeline.run_job", blocked_run)

    tasks_module.process_ingest_job.call_local(job.id)
    waiting = ingest.get_job(migrated_db, job.id)
    assert waiting is not None and ingest.is_waiting_retry(waiting)
    assert scheduled == [((job.id, 1), 1800)]

    tasks_module.process_ingest_job.call_local(job.id, 1)
    tasks_module.process_ingest_job.call_local(job.id, 2)
    assert [d for _, d in scheduled] == [1800, 7200, 21600]

    # schedule exhausted: the job stays failed and nothing more is scheduled
    tasks_module.process_ingest_job.call_local(job.id, 3)
    final = ingest.get_job(migrated_db, job.id)
    assert final is not None and final.status == "failed"
    assert final.error_category == "youtube_blocked"
    assert len(scheduled) == 3


def test_task_skips_stale_scheduled_retry(
    migrated_db: sqlite3.Connection, tasks_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = ingest.enqueue_job(migrated_db, _URL)
    ingest.set_status(migrated_db, job.id, "failed", error_category="fetch", error_message="x")
    ran: list[int] = []
    monkeypatch.setattr("app.services.pipeline.run_job", lambda conn, j: ran.append(j.id))
    tasks_module.process_ingest_job.call_local(job.id, 2)  # not waiting any more -> no-op
    assert ran == []
