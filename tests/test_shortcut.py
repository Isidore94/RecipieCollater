"""The 'share from your phone' setup: instructions page + minting a working ingest token."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient


def test_shortcut_page_shows_endpoint_and_steps(admin_client: TestClient) -> None:
    body = admin_client.get("/shortcut").text
    assert "Share from your phone" in body
    assert "/api/ingest" in body  # the endpoint the Shortcut posts to
    assert "Authorization" in body  # header instructions
    assert "outerHTML" in body  # the JS-in-Safari HTML-capture variant


def test_create_token_reveals_a_working_ingest_token(admin_client: TestClient) -> None:
    resp = admin_client.post("/shortcut/token", data={"label": "My iPhone"})
    assert resp.status_code == 200
    match = re.search(r'class="shortcut-token"[^>]*value="([^"]+)"', resp.text)
    assert match, "the freshly minted token should be shown once"
    raw = match.group(1)

    # The token the page shows must actually authenticate the ingest API.
    ingest = admin_client.post(
        "/api/ingest",
        json={"url": "https://example.test/from-shortcut"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert ingest.status_code == 202


def test_shortcut_requires_login(client: TestClient) -> None:
    # No session -> the page is not served as the setup UI (auth gate kicks in).
    resp = client.get("/shortcut", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307, 401)
