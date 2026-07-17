"""Route-level tests: onboarding, CSRF enforcement, admin gating, and proof that an
ingest token grants zero browser/app access."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.security import SESSION_COOKIE_NAME
from app.services import sessions, tokens
from app.services.users import create_user
from tests.conftest import SAME_ORIGIN


def _app_db() -> sqlite3.Connection:
    return connect(config.get_settings().db_path)


def test_healthz_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["schema"] == "current"
    assert body["checks"]["queue_db"] == "ok"


def test_first_run_shows_setup(client: TestClient) -> None:
    r = client.get("/welcome")
    assert r.status_code == 200
    assert "set up" in r.text.lower()


def test_protected_page_redirects_without_session(client: TestClient) -> None:
    r = client.get("/inbox", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/welcome"


def test_root_redirects(admin_client: TestClient) -> None:
    r = admin_client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/inbox"


def test_setup_then_access(admin_client: TestClient) -> None:
    r = admin_client.get("/inbox")
    assert r.status_code == 200
    assert "Test Recipes" in r.text


def test_second_setup_is_refused(admin_client: TestClient) -> None:
    # A second /setup must not create another admin once the app is initialised.
    r = admin_client.post(
        "/setup", data={"admin_name": "Intruder"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert r.status_code == 303
    conn = _app_db()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_csrf_blocks_cross_site_post(client: TestClient) -> None:
    r = client.post(
        "/setup",
        data={"admin_name": "Aaron"},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_theme_toggle_sets_cookie(admin_client: TestClient) -> None:
    r = admin_client.post(
        "/theme", data={"theme": "dark"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert r.status_code == 303
    assert "rc_theme=dark" in r.headers.get("set-cookie", "")


def test_admin_devices_visible_to_admin(admin_client: TestClient) -> None:
    r = admin_client.get("/admin/devices")
    assert r.status_code == 200
    assert "Devices" in r.text


def test_non_admin_cannot_reach_admin(admin_client: TestClient) -> None:
    # Create a non-admin user + session directly, then use that cookie in a fresh client.
    conn = _app_db()
    try:
        sam = create_user(conn, "Sam", is_admin=False)
        sam_session = sessions.create_session(conn, sam.id, "Sam iPhone")
    finally:
        conn.close()

    from app.main import create_app

    with TestClient(create_app()) as sam_client:
        sam_client.cookies.set(SESSION_COOKIE_NAME, sam_session)
        # Sam can use the app...
        assert sam_client.get("/inbox").status_code == 200
        # ...but not the admin area.
        r = sam_client.get("/admin/devices", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/welcome"


def test_ingest_token_has_no_app_access(admin_client: TestClient) -> None:
    """An ingest-only token must not open any browser/app route (CONVENTIONS §6)."""
    conn = _app_db()
    try:
        user = create_user(conn, "Bot", is_admin=False)
        ingest_raw = tokens.create_ingest_token(conn, user.id, "Shortcut")
    finally:
        conn.close()

    from app.main import create_app

    with TestClient(create_app()) as bot_client:
        headers = {"authorization": f"Bearer {ingest_raw}"}
        for path in ("/inbox", "/cookbook", "/pantry", "/plan", "/chat", "/admin/devices"):
            r = bot_client.get(path, headers=headers, follow_redirects=False)
            assert r.status_code == 303, f"{path} should deny an ingest token"
            assert r.headers["location"] == "/welcome"


def test_pairing_flow_via_magic_link(admin_client: TestClient) -> None:
    # Admin invites a device for a new user; the magic link pairs a fresh client.
    conn = _app_db()
    try:
        sam = create_user(conn, "Sam", is_admin=False)
    finally:
        conn.close()
    r = admin_client.post(
        "/admin/devices/invite",
        data={"user_id": str(sam.id), "device_name": "Sam iPhone"},
        headers=SAME_ORIGIN,
    )
    assert r.status_code == 200
    # Extract the magic-link token from the rendered page.
    assert "/pair?t=" in r.text
    token = r.text.split("/pair?t=")[1].split("<")[0].strip()

    from app.main import create_app

    with TestClient(create_app()) as sam_client:
        paired = sam_client.get(f"/pair?t={token}", follow_redirects=False)
        assert paired.status_code == 303
        assert "rc_session" in paired.headers.get("set-cookie", "")
        assert sam_client.get("/inbox").status_code == 200


def test_revoke_session_blocks_further_access(admin_client: TestClient) -> None:
    # Pair Sam, then revoke and confirm access is cut.
    conn = _app_db()
    try:
        sam = create_user(conn, "Sam", is_admin=False)
        sam_session_raw = sessions.create_session(conn, sam.id, "Sam iPhone")
        session_id = next(s.id for s in sessions.list_sessions(conn) if s.user_id == sam.id)
    finally:
        conn.close()

    from app.main import create_app

    with TestClient(create_app()) as sam_client:
        sam_client.cookies.set(SESSION_COOKIE_NAME, sam_session_raw)
        assert sam_client.get("/inbox").status_code == 200

    admin_client.post(
        f"/admin/devices/session/{session_id}/revoke", headers=SAME_ORIGIN, follow_redirects=False
    )

    with TestClient(create_app()) as sam_client:
        sam_client.cookies.set(SESSION_COOKIE_NAME, sam_session_raw)
        r = sam_client.get("/inbox", follow_redirects=False)
        assert r.status_code == 303
