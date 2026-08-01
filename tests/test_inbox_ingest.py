"""The browser ingest flow: the inbox paste box, job creation, and the self-polling job list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.services import ingest


def test_inbox_shows_paste_box(admin_client: TestClient) -> None:
    body = admin_client.get("/inbox").text
    assert "Add a recipe from the web" in body
    assert 'action="/inbox/ingest"' in body


def test_browser_ingest_creates_job_and_redirects(admin_client: TestClient) -> None:
    resp = admin_client.post(
        "/inbox/ingest", data={"url": "https://example.test/soup"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inbox"

    conn = connect(config.get_settings().db_path)
    try:
        jobs = ingest.list_pending_jobs(conn)
    finally:
        conn.close()
    assert [j.url for j in jobs] == ["https://example.test/soup"]
    assert jobs[0].source == "paste"


def test_browser_ingest_rejects_empty_url(admin_client: TestClient) -> None:
    resp = admin_client.post("/inbox/ingest", data={"url": "   "})
    assert resp.status_code == 400
    assert "Enter a recipe link" in resp.text


def test_browser_ingest_rejects_bad_scheme(admin_client: TestClient) -> None:
    resp = admin_client.post("/inbox/ingest", data={"url": "ftp://x/y"})
    assert resp.status_code == 400


def test_inbox_jobs_fragment_polls_while_active(admin_client: TestClient) -> None:
    admin_client.post("/inbox/ingest", data={"url": "https://example.test/stew"})
    frag = admin_client.get("/inbox/jobs")
    assert frag.status_code == 200
    assert 'id="ingest-jobs"' in frag.text
    assert "https://example.test/stew" in frag.text
    # a queued job is active, so the fragment must keep polling itself
    assert 'hx-trigger="every 3s"' in frag.text


def _only_job_id() -> int:
    conn = connect(config.get_settings().db_path)
    try:
        return ingest.list_pending_jobs(conn)[0].id
    finally:
        conn.close()


def _fail_the_job(job_id: int, message: str = "the site returned HTTP 402") -> None:
    conn = connect(config.get_settings().db_path)
    try:
        ingest.set_status(conn, job_id, "failed", error_category="fetch", error_message=message)
    finally:
        conn.close()


def test_failed_job_offers_retry_and_dismiss(admin_client: TestClient) -> None:
    """A failed paste used to sit in the inbox forever with a raw error and no way out."""
    admin_client.post("/inbox/ingest", data={"url": "https://example.test/paywalled"})
    job_id = _only_job_id()
    _fail_the_job(job_id)

    frag = admin_client.get("/inbox/jobs").text
    assert "the site returned HTTP 402" in frag
    assert f"/inbox/jobs/{job_id}/retry" in frag
    assert f"/inbox/jobs/{job_id}/dismiss" in frag
    # Nothing is in flight, so the fragment must stop polling.
    assert 'hx-trigger="every 3s"' not in frag


def test_retry_requeues_a_failed_job(admin_client: TestClient) -> None:
    admin_client.post("/inbox/ingest", data={"url": "https://example.test/flaky"})
    job_id = _only_job_id()
    _fail_the_job(job_id)

    resp = admin_client.post(f"/inbox/jobs/{job_id}/retry", follow_redirects=False)
    assert resp.status_code == 303
    conn = connect(config.get_settings().db_path)
    try:
        job = ingest.get_job(conn, job_id)
        assert job is not None
        assert job.status == "queued" and job.error_message is None
    finally:
        conn.close()


def test_dismiss_removes_a_failed_job(admin_client: TestClient) -> None:
    admin_client.post("/inbox/ingest", data={"url": "https://example.test/giveup"})
    job_id = _only_job_id()
    _fail_the_job(job_id)

    admin_client.post(f"/inbox/jobs/{job_id}/dismiss", follow_redirects=False)
    conn = connect(config.get_settings().db_path)
    try:
        assert ingest.get_job(conn, job_id) is None
    finally:
        conn.close()


def test_retry_and_dismiss_ignore_a_job_that_is_not_failed(admin_client: TestClient) -> None:
    """Only a failed job may be requeued or deleted; an in-flight one must be left alone."""
    admin_client.post("/inbox/ingest", data={"url": "https://example.test/running"})
    job_id = _only_job_id()
    conn = connect(config.get_settings().db_path)
    try:
        ingest.set_status(conn, job_id, "fetching")
        assert ingest.requeue_failed(conn, job_id) is False
        assert ingest.discard_failed(conn, job_id) is False
        job = ingest.get_job(conn, job_id)
        assert job is not None and job.status == "fetching"
    finally:
        conn.close()


def test_finished_job_announces_the_recipe_it_added(admin_client: TestClient) -> None:
    """Success was invisible: the job just vanished and a card appeared further down."""
    from app.services import recipes

    admin_client.post("/inbox/ingest", data={"url": "https://example.test/pie"})
    job_id = _only_job_id()
    conn = connect(config.get_settings().db_path)
    try:
        recipe_id = recipes.create_recipe(
            conn, recipes.RecipeInput(title="Apple Pie", base_servings="4")
        )
        ingest.set_status(conn, job_id, "done", recipe_id=recipe_id)
    finally:
        conn.close()

    frag = admin_client.get("/inbox/jobs").text
    assert "Added" in frag and "Apple Pie" in frag
    assert 'href="/recipes/apple-pie"' in frag
