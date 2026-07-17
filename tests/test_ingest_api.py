"""POST /api/ingest: 202 + job id, idempotency, and the ingest-token scope boundary."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.services import tokens
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
