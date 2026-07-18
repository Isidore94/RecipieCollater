"""Ingestion service: URL normalization, idempotent enqueue, artifacts, and lifecycle."""

from __future__ import annotations

import sqlite3

import pytest

from app.services import ingest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/recipe/", "https://example.com/recipe"),
        ("https://Example.com:443/A/b?x=1", "https://example.com/A/b?x=1"),
        ("example.com/r", "https://example.com/r"),
        ("https://site.com/r?utm_source=fb&id=9", "https://site.com/r?id=9"),
        ("https://site.com/r#frag", "https://site.com/r"),
        ("https://www.youtube.com/watch?v=abc123&t=30", "https://www.youtube.com/watch?v=abc123"),
        ("https://youtu.be/abc123", "https://www.youtube.com/watch?v=abc123"),
        ("https://m.youtube.com/shorts/xyz789", "https://www.youtube.com/watch?v=xyz789"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert ingest.normalize_url(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "ftp://x/y", "javascript:alert(1)", "https://"])
def test_normalize_url_rejects(bad: str) -> None:
    with pytest.raises(ingest.IngestError):
        ingest.normalize_url(bad)


def test_youtube_video_id() -> None:
    assert ingest.youtube_video_id("https://youtu.be/abc123") == "abc123"
    assert ingest.youtube_video_id("https://www.youtube.com/watch?v=xyz") == "xyz"
    assert ingest.youtube_video_id("https://example.com/watch?v=x") is None


def test_enqueue_is_idempotent(migrated_db: sqlite3.Connection) -> None:
    job1, created1 = ingest.enqueue_job(migrated_db, "https://site.com/r?utm_source=x")
    assert created1 is True
    job2, created2 = ingest.enqueue_job(migrated_db, "https://site.com/r")
    assert created2 is False
    assert job2.id == job1.id
    assert migrated_db.execute("SELECT COUNT(*) FROM ingest_jobs").fetchone()[0] == 1


def test_supplied_html_stored_as_artifact(migrated_db: sqlite3.Connection) -> None:
    job, _ = ingest.enqueue_job(migrated_db, "https://site.com/r", html="<html>hi</html>")
    assert job.has_html is True
    assert ingest.read_artifact(migrated_db, job.id, "supplied_html") == b"<html>hi</html>"


def test_artifact_is_content_addressed(migrated_db: sqlite3.Connection) -> None:
    job, _ = ingest.enqueue_job(migrated_db, "https://site.com/r")
    sha_a = ingest.store_artifact(migrated_db, job.id, "fetched_html", b"same bytes")
    sha_b = ingest.store_artifact(migrated_db, job.id, "json_ld", b"same bytes")
    assert sha_a == sha_b  # identical content -> identical hash / same blob
    assert ingest.read_artifact(migrated_db, job.id, "fetched_html") == b"same bytes"


def test_lifecycle_and_active_jobs(migrated_db: sqlite3.Connection) -> None:
    job, _ = ingest.enqueue_job(migrated_db, "https://site.com/r")
    assert [j.id for j in ingest.list_active_jobs(migrated_db)] == [job.id]
    ingest.set_status(migrated_db, job.id, "fetching")
    assert ingest.increment_attempts(migrated_db, job.id) == 1
    ingest.set_status(
        migrated_db, job.id, "failed", error_category="fetch_blocked", error_message="403"
    )
    done = ingest.get_job(migrated_db, job.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error_category == "fetch_blocked"
    assert ingest.list_active_jobs(migrated_db) == []


def test_repasting_a_failed_url_requeues_it(migrated_db: sqlite3.Connection) -> None:
    """A failed URL must not be permanently stuck: pasting it again means "try again"."""
    job, created = ingest.enqueue_job(migrated_db, "https://example.test/flaky")
    assert created is True
    ingest.set_status(
        migrated_db, job.id, "failed", error_category="no_recipe", error_message="nope"
    )

    again, created_again = ingest.enqueue_job(migrated_db, "https://example.test/flaky")
    assert created_again is True  # so the router schedules processing again
    assert again.id == job.id  # same job row, reset - not a duplicate
    assert again.status == "queued"
    assert again.error_category is None and again.error_message is None

    # a job that is NOT failed still behaves idempotently
    third, created_third = ingest.enqueue_job(migrated_db, "https://example.test/flaky")
    assert created_third is False and third.status == "queued"
