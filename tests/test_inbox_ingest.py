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
