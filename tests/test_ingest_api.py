"""POST /api/ingest: 202 + job id, idempotency, and the ingest-token scope boundary."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.services import ingest, recipes, tokens
from app.services.users import create_user


def _ingest_token() -> str:
    conn = connect(config.get_settings().db_path)
    try:
        user = create_user(conn, "Aaron", is_admin=True)
        return tokens.create_ingest_token(conn, user.id, "Shortcut")
    finally:
        conn.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_ingest_returns_202_with_job(client: TestClient) -> None:
    resp = client.post(
        "/api/ingest", json={"url": "https://example.com/soup"}, headers=_auth(_ingest_token())
    )
    assert resp.status_code == 202
    body = resp.json()
    assert isinstance(body["job_id"], int)
    assert body["status"] == "queued"
    assert body["duplicate"] is False


def test_duplicate_url_is_flagged(client: TestClient) -> None:
    token = _ingest_token()
    first = client.post(
        "/api/ingest", json={"url": "https://example.com/soup"}, headers=_auth(token)
    )
    second = client.post(
        "/api/ingest", json={"url": "https://example.com/soup?utm_source=x"}, headers=_auth(token)
    )
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["job_id"] == first.json()["job_id"]


def test_ingest_requires_token_not_cookie(admin_client: TestClient) -> None:
    # A browser cookie session must never authenticate the ingest API (CONVENTIONS 6).
    resp = admin_client.post("/api/ingest", json={"url": "https://example.com/x"})
    assert resp.status_code == 401


def test_ingest_rejects_bad_body(client: TestClient) -> None:
    token = _ingest_token()
    assert client.post("/api/ingest", json={"nope": 1}, headers=_auth(token)).status_code == 400
    assert client.post("/api/ingest", json={"url": "   "}, headers=_auth(token)).status_code == 400
    ftp = client.post("/api/ingest", json={"url": "ftp://x/y"}, headers=_auth(token))
    assert ftp.status_code == 400


def _post_job(client: TestClient, token: str, url: str = "https://example.com/soup") -> dict:  # type: ignore[type-arg]
    resp = client.post("/api/ingest", json={"url": url}, headers=_auth(token))
    assert resp.status_code == 202
    return resp.json()  # type: ignore[no-any-return]


def test_post_returns_absolute_status_url(client: TestClient) -> None:
    body = _post_job(client, _ingest_token())
    base = config.get_settings().app_base_url
    assert body["status_url"] == f"{base}/api/ingest/{body['job_id']}"


@pytest.mark.parametrize(
    ("db_status", "expected"),
    [
        ("queued", "queued"),
        ("fetching", "fetching"),
        ("extracting", "extracting"),
        ("normalizing", "extracting"),
    ],
)
def test_status_in_flight_states(client: TestClient, db_status: str, expected: str) -> None:
    token = _ingest_token()
    job_id = _post_job(client, token)["job_id"]
    conn = connect(config.get_settings().db_path)
    try:
        ingest.set_status(conn, job_id, db_status)
    finally:
        conn.close()
    resp = client.get(f"/api/ingest/{job_id}", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == expected
    assert body["recipe_id"] is None
    assert body["recipe_title"] is None
    assert body["recipe_url"] is None
    assert body["error_message"] is None


def test_status_done_has_recipe_title_and_absolute_url(client: TestClient) -> None:
    token = _ingest_token()
    job_id = _post_job(client, token)["job_id"]
    conn = connect(config.get_settings().db_path)
    try:
        rid = recipes.create_recipe(conn, recipes.RecipeInput(title="Tomato Soup"))
        slug = conn.execute("SELECT slug FROM recipes WHERE id = ?", (rid,)).fetchone()["slug"]
        ingest.set_status(conn, job_id, "done", recipe_id=rid)
    finally:
        conn.close()
    body = client.get(f"/api/ingest/{job_id}", headers=_auth(token)).json()
    assert body["status"] == "done"
    assert body["recipe_id"] == rid
    assert body["recipe_title"] == "Tomato Soup"
    assert body["recipe_url"] == f"{config.get_settings().app_base_url}/recipes/{slug}"


def test_status_failed_has_error(client: TestClient) -> None:
    token = _ingest_token()
    job_id = _post_job(client, token)["job_id"]
    conn = connect(config.get_settings().db_path)
    try:
        ingest.set_status(
            conn, job_id, "failed", error_category="fetch", error_message="Site said no"
        )
    finally:
        conn.close()
    body = client.get(f"/api/ingest/{job_id}", headers=_auth(token)).json()
    assert body["status"] == "failed"
    assert body["error_category"] == "fetch"
    assert body["error_message"] == "Site said no"
    assert body["recipe_url"] is None


def test_status_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/api/ingest/9999", headers=_auth(_ingest_token())).status_code == 404


def test_status_of_another_users_job_is_404(client: TestClient) -> None:
    owner_token = _ingest_token()
    job_id = _post_job(client, owner_token)["job_id"]
    conn = connect(config.get_settings().db_path)
    try:
        other = create_user(conn, "Sam")
        other_token = tokens.create_ingest_token(conn, other.id, "Shortcut")
    finally:
        conn.close()
    assert client.get(f"/api/ingest/{job_id}", headers=_auth(other_token)).status_code == 404
    assert client.get(f"/api/ingest/{job_id}", headers=_auth(owner_token)).status_code == 200


def test_status_requires_token(client: TestClient) -> None:
    job_id = _post_job(client, _ingest_token())["job_id"]
    assert client.get(f"/api/ingest/{job_id}").status_code == 401
    bad = client.get(f"/api/ingest/{job_id}", headers=_auth("not-a-token"))
    assert bad.status_code == 401


def test_status_rejects_cookie_session_like_the_post(admin_client: TestClient) -> None:
    # Same scope boundary as the POST: a browser cookie never authenticates the ingest API.
    assert admin_client.get("/api/ingest/1").status_code == 401
