"""The /add page: the PC counterpart of the iPhone Shortcut (docs/04 section 1.3).

Covers the paste form, the ?url= prefill the bookmarklet lands on, the bookmarklet itself, the
duplicate-resubmit message, and the rule that a GET never queues a job.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.routers.pages import bookmarklet
from app.services import ingest


def _jobs() -> list[ingest.IngestJob]:
    conn = connect(config.get_settings().db_path)
    try:
        return ingest.list_pending_jobs(conn)
    finally:
        conn.close()


def test_add_page_offers_a_link_box(admin_client: TestClient) -> None:
    body = admin_client.get("/add").text
    assert "Paste a YouTube or recipe-website link" in body
    assert 'action="/add"' in body


def test_add_page_is_reachable_from_every_page(admin_client: TestClient) -> None:
    """Tier 2.4 of the usability review: the add surface used to exist only on Inbox."""
    for path in ("/", "/cookbook", "/pantry"):
        body = admin_client.get(path).text
        assert 'href="/add"' in body, path


def test_add_creates_a_job_and_returns_to_the_page(admin_client: TestClient) -> None:
    resp = admin_client.post(
        "/add", data={"url": "https://example.test/laksa"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/add?notice=")

    jobs = _jobs()
    assert [j.url for j in jobs] == ["https://example.test/laksa"]
    assert jobs[0].source == "paste"


def test_add_accepts_a_youtube_link(admin_client: TestClient) -> None:
    admin_client.post("/add", data={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert _jobs()[0].normalized_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_add_carries_pasted_html_for_a_site_that_blocks_fetching(
    admin_client: TestClient,
) -> None:
    admin_client.post(
        "/add",
        data={"url": "https://example.test/blocked", "html": "<html>soup</html>"},
    )
    job = _jobs()[0]
    assert job.has_html
    conn = connect(config.get_settings().db_path)
    try:
        assert ingest.read_artifact(conn, job.id, "supplied_html") == b"<html>soup</html>"
    finally:
        conn.close()


def test_add_rejects_an_empty_url(admin_client: TestClient) -> None:
    resp = admin_client.post("/add", data={"url": "   "})
    assert resp.status_code == 400
    assert "Enter a recipe link" in resp.text
    assert _jobs() == []


def test_add_rejects_a_bad_scheme_and_keeps_what_was_typed(admin_client: TestClient) -> None:
    resp = admin_client.post("/add", data={"url": "ftp://x/y"})
    assert resp.status_code == 400
    assert "ftp://x/y" in resp.text  # re-shown so it can be corrected, not silently cleared
    assert _jobs() == []


def test_resubmitting_a_known_link_says_so(admin_client: TestClient) -> None:
    """Idempotency means no second job - which looked like a dead button."""
    admin_client.post("/add", data={"url": "https://example.test/again"})
    resp = admin_client.post(
        "/add", data={"url": "https://example.test/again"}, follow_redirects=True
    )
    assert "already being read" in resp.text


def test_resubmitting_a_finished_link_names_the_recipe(admin_client: TestClient) -> None:
    from app.services import recipes

    admin_client.post("/add", data={"url": "https://example.test/pie"})
    conn = connect(config.get_settings().db_path)
    try:
        job_id = ingest.list_pending_jobs(conn)[0].id
        recipe_id = recipes.create_recipe(
            conn, recipes.RecipeInput(title="Apple Pie", base_servings="4")
        )
        ingest.set_status(conn, job_id, "done", recipe_id=recipe_id)
    finally:
        conn.close()

    resp = admin_client.post(
        "/add", data={"url": "https://example.test/pie"}, follow_redirects=True
    )
    assert "Already added: Apple Pie" in resp.text


def test_bookmarklet_prefills_the_box_without_queueing(admin_client: TestClient) -> None:
    """A GET must never mutate (CONVENTIONS 7): the bookmarklet fills the field, then waits."""
    body = admin_client.get("/add?url=https://example.test/carbonara").text
    assert 'value="https://example.test/carbonara"' in body
    assert _jobs() == []


def test_prefill_drops_a_script_bearing_url(admin_client: TestClient) -> None:
    body = admin_client.get("/add?url=javascript:alert(1)").text
    assert "javascript:alert(1)" not in body


def test_bookmarklet_is_built_from_the_configured_base_url(admin_client: TestClient) -> None:
    base = config.get_settings().app_base_url
    assert bookmarklet(base) == (
        "javascript:void(window.open('"
        + base.rstrip("/")
        + "/add?url='+encodeURIComponent(location.href),'_blank'))"
    )
    # And the page ships it, so there is no second copy of the host in a template
    # (CONVENTIONS 11).
    assert base.rstrip("/") + "/add?url=" in admin_client.get("/add").text


def test_add_page_shows_job_progress(admin_client: TestClient) -> None:
    admin_client.post("/add", data={"url": "https://example.test/ramen"})
    body = admin_client.get("/add").text
    assert 'id="ingest-jobs"' in body
    assert "https://example.test/ramen" in body
    assert 'hx-trigger="every 3s"' in body


def test_add_requires_a_signed_in_user(client: TestClient) -> None:
    for resp in (
        client.get("/add", follow_redirects=False),
        client.post("/add", data={"url": "https://example.test/x"}, follow_redirects=False),
    ):
        assert resp.status_code in (302, 303, 307, 401, 403)
        assert "/add" not in resp.headers.get("location", "")
