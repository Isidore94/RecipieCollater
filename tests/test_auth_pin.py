"""Route tests for PIN sign-in and admin PIN management (CONVENTIONS §5-§7, §15)."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.services import credentials, sessions
from app.services.users import create_user, get_user_by_name

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def _db() -> sqlite3.Connection:
    return connect(config.get_settings().db_path)


def _make_user_with_pin(name: str, pin: str, *, admin: bool = False) -> int:
    conn = _db()
    try:
        user = create_user(conn, name, is_admin=admin)
        credentials.set_pin(conn, user.id, pin)
        return user.id
    finally:
        conn.close()


def test_login_form_renders(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_login_success(client: TestClient) -> None:
    _make_user_with_pin("Bea", "2468")
    resp = client.post(
        "/login", data={"name": "Bea", "pin": "2468"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "rc_session" in resp.headers.get("set-cookie", "")
    # the freshly minted device session grants access
    assert client.get("/inbox").status_code == 200


def test_login_wrong_pin_is_401_without_cookie(client: TestClient) -> None:
    _make_user_with_pin("Bea", "2468")
    resp = client.post("/login", data={"name": "Bea", "pin": "0000"}, headers=SAME_ORIGIN)
    assert resp.status_code == 401
    assert "rc_session" not in resp.headers.get("set-cookie", "")


def test_login_blocks_cross_site(client: TestClient) -> None:
    _make_user_with_pin("Bea", "2468")
    # An explicit cross-site POST is rejected; SameSite=Lax also withholds the cookie.
    resp = client.post(
        "/login", data={"name": "Bea", "pin": "2468"}, headers={"sec-fetch-site": "cross-site"}
    )
    assert resp.status_code == 403


def test_login_lockout_after_repeated_failures(client: TestClient) -> None:
    _make_user_with_pin("Bea", "2468")
    for _ in range(credentials.MAX_FAILED_ATTEMPTS - 1):
        r = client.post("/login", data={"name": "Bea", "pin": "0000"}, headers=SAME_ORIGIN)
        assert r.status_code == 401
    tripped = client.post("/login", data={"name": "Bea", "pin": "0000"}, headers=SAME_ORIGIN)
    assert tripped.status_code == 429
    # even the correct PIN is refused while locked out
    locked = client.post(
        "/login", data={"name": "Bea", "pin": "2468"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert locked.status_code == 429


def test_admin_sets_pin(admin_client: TestClient) -> None:
    admin_client.post("/admin/users", data={"name": "Bea"}, headers=SAME_ORIGIN)
    conn = _db()
    try:
        bea = get_user_by_name(conn, "Bea")
        assert bea is not None
    finally:
        conn.close()
    resp = admin_client.post(
        f"/admin/users/{bea.id}/pin", data={"pin": "2468"}, headers=SAME_ORIGIN
    )
    assert resp.status_code == 200
    conn = _db()
    try:
        assert credentials.has_pin(conn, bea.id)
    finally:
        conn.close()


def test_admin_rejects_bad_pin(admin_client: TestClient) -> None:
    admin_client.post("/admin/users", data={"name": "Bea"}, headers=SAME_ORIGIN)
    conn = _db()
    try:
        bea = get_user_by_name(conn, "Bea")
        assert bea is not None
    finally:
        conn.close()
    resp = admin_client.post(f"/admin/users/{bea.id}/pin", data={"pin": "12"}, headers=SAME_ORIGIN)
    assert resp.status_code == 200  # re-renders with an error, does not set a PIN
    conn = _db()
    try:
        assert not credentials.has_pin(conn, bea.id)
    finally:
        conn.close()


def test_non_admin_cannot_set_pin(client: TestClient) -> None:
    bea_id = _make_user_with_pin("Bea", "2468")
    conn = _db()
    try:
        raw = sessions.create_session(conn, bea_id, "Bea Phone")
    finally:
        conn.close()
    client.cookies.set("rc_session", raw)
    resp = client.post(
        f"/admin/users/{bea_id}/pin",
        data={"pin": "9999"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/welcome"
