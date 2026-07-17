"""Auth boundaries: session lifecycle, and the hard separation between browser
identity (cookie) and ingest identity (scoped Bearer token) (CONVENTIONS §5, §6)."""

from __future__ import annotations

import sqlite3

import pytest
from starlette.requests import Request

from app.auth import CSRFError, IngestAuthError, require_csrf, require_ingest_token
from app.security import SESSION_COOKIE_NAME, hash_token
from app.services import onboarding, sessions, tokens
from app.services.users import create_user


def _make_request(headers: dict[str, str], path: str = "/api/ingest") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": raw,
        "query_string": b"",
    }
    return Request(scope)


# ---- Session lifecycle ----------------------------------------------------------------


def test_session_create_and_resolve(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron", is_admin=True)
    raw = sessions.create_session(migrated_db, user.id, "Kitchen PC")
    resolved = sessions.resolve_session(migrated_db, raw)
    assert resolved is not None
    assert resolved.session.user_id == user.id
    assert resolved.renewed is False


def test_recent_session_read_does_not_write(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = sessions.create_session(migrated_db, user.id, "PC")
    changes_before = migrated_db.total_changes
    assert sessions.resolve_session(migrated_db, raw) is not None
    assert migrated_db.total_changes == changes_before


def test_session_only_hash_is_stored(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = sessions.create_session(migrated_db, user.id, "PC")
    row = migrated_db.execute("SELECT token_hash FROM device_sessions").fetchone()
    assert row["token_hash"] == hash_token(raw)
    assert raw not in row["token_hash"]


def test_unknown_and_revoked_sessions_do_not_resolve(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = sessions.create_session(migrated_db, user.id, "PC")
    assert sessions.resolve_session(migrated_db, "not-a-token") is None
    session_id = sessions.list_sessions(migrated_db)[0].id
    assert sessions.revoke_session(migrated_db, session_id) is True
    assert sessions.resolve_session(migrated_db, raw) is None


def test_expired_session_does_not_resolve(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = sessions.create_session(migrated_db, user.id, "PC")
    migrated_db.execute("UPDATE device_sessions SET expires_at = '2000-01-01 00:00:00'")
    migrated_db.commit()
    assert sessions.resolve_session(migrated_db, raw) is None


def test_sliding_renewal_extends_expiry(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = sessions.create_session(migrated_db, user.id, "PC")
    # Age the session beyond the renewal threshold.
    migrated_db.execute(
        """UPDATE device_sessions
           SET issued_at = '2020-01-01 00:00:00', renewed_at = '2020-01-01 00:00:00'"""
    )
    migrated_db.commit()
    resolved = sessions.resolve_session(migrated_db, raw)
    assert resolved is not None
    assert resolved.renewed is True
    # The renewal timestamp was advanced, so the next request does not renew again.
    again = sessions.resolve_session(migrated_db, raw)
    assert again is not None
    assert again.renewed is False


# ---- Ingest-token scope boundary (both directions) ------------------------------------


def test_ingest_token_resolves_only_with_ingest_scope(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = tokens.create_ingest_token(migrated_db, user.id, "Shortcut")
    assert tokens.resolve_token(migrated_db, raw, required_scope="ingest") is not None
    # A scope the system does not grant never resolves.
    assert tokens.resolve_token(migrated_db, raw, required_scope="admin") is None


def test_revoked_ingest_token_denied(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    raw = tokens.create_ingest_token(migrated_db, user.id, "Shortcut")
    token_id = tokens.list_tokens(migrated_db)[0].id
    assert tokens.revoke_token(migrated_db, token_id) is True
    assert tokens.resolve_token(migrated_db, raw, required_scope="ingest") is None


def test_session_token_is_not_an_ingest_token(migrated_db: sqlite3.Connection) -> None:
    """A browser session token must never authenticate the ingest API."""
    user = create_user(migrated_db, "Aaron")
    session_raw = sessions.create_session(migrated_db, user.id, "PC")
    assert tokens.resolve_token(migrated_db, session_raw, required_scope="ingest") is None


def test_ingest_token_is_not_a_session(migrated_db: sqlite3.Connection) -> None:
    """An ingest token must never authenticate a browser session."""
    user = create_user(migrated_db, "Aaron")
    ingest_raw = tokens.create_ingest_token(migrated_db, user.id, "Shortcut")
    assert sessions.resolve_session(migrated_db, ingest_raw) is None


def test_require_ingest_token_accepts_bearer_only(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    ingest_raw = tokens.create_ingest_token(migrated_db, user.id, "Shortcut")
    req = _make_request({"authorization": f"Bearer {ingest_raw}"})
    token = require_ingest_token(req, migrated_db)
    assert token.scope == "ingest"


def test_require_ingest_token_rejects_cookie(migrated_db: sqlite3.Connection) -> None:
    """A cookie (even a valid session) must not satisfy the ingest dependency."""
    user = create_user(migrated_db, "Aaron")
    session_raw = sessions.create_session(migrated_db, user.id, "PC")
    req = _make_request({"cookie": f"{SESSION_COOKIE_NAME}={session_raw}"})
    with pytest.raises(IngestAuthError):
        require_ingest_token(req, migrated_db)


def test_require_ingest_token_rejects_missing_and_garbage(migrated_db: sqlite3.Connection) -> None:
    with pytest.raises(IngestAuthError):
        require_ingest_token(_make_request({}), migrated_db)
    with pytest.raises(IngestAuthError):
        require_ingest_token(_make_request({"authorization": "Bearer nope"}), migrated_db)
    with pytest.raises(IngestAuthError):
        require_ingest_token(_make_request({"authorization": "Basic abc"}), migrated_db)


# ---- Onboarding tokens ----------------------------------------------------------------


def test_magic_link_single_use(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    issued = onboarding.issue_magic_link(migrated_db, user.id, "Sam iPhone")
    first = onboarding.consume(migrated_db, issued.raw, kind="magic_link")
    assert first is not None and first.user_id == user.id
    # Replay is rejected.
    assert onboarding.consume(migrated_db, issued.raw, kind="magic_link") is None


def test_onboarding_wrong_kind_rejected(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    issued = onboarding.issue_pairing_code(migrated_db, user.id, None)
    # A pairing code must not be consumable as a magic link.
    assert onboarding.consume(migrated_db, issued.raw, kind="magic_link") is None
    assert onboarding.consume(migrated_db, issued.raw, kind="pairing_code") is not None


def test_expired_onboarding_rejected(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    issued = onboarding.issue_magic_link(migrated_db, user.id, None)
    migrated_db.execute("UPDATE onboarding_tokens SET expires_at = '2000-01-01 00:00:00'")
    migrated_db.commit()
    assert onboarding.consume(migrated_db, issued.raw, kind="magic_link") is None


def test_pairing_code_is_case_insensitive(migrated_db: sqlite3.Connection) -> None:
    user = create_user(migrated_db, "Aaron")
    issued = onboarding.issue_pairing_code(migrated_db, user.id, None)
    assert onboarding.consume(migrated_db, issued.raw.lower(), kind="pairing_code") is not None


def test_first_admin_bootstrap_is_single_winner(migrated_db: sqlite3.Connection) -> None:
    first = onboarding.bootstrap_first_admin(migrated_db, name="Aaron", device_name="PC")
    second = onboarding.bootstrap_first_admin(migrated_db, name="Intruder", device_name="Other")
    assert first is not None
    assert second is None
    assert migrated_db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


# ---- CSRF dependency ------------------------------------------------------------------


def test_csrf_allows_same_origin_none_and_absent() -> None:
    # Same-origin, user-initiated, same-site, htmx, and browsers that omit Fetch Metadata over
    # plain HTTP are all allowed; SameSite=Lax is the CSRF guarantee for the absent case.
    require_csrf(_make_request({"sec-fetch-site": "same-origin"}))
    require_csrf(_make_request({"sec-fetch-site": "none"}))
    require_csrf(_make_request({"sec-fetch-site": "same-site"}))
    require_csrf(_make_request({"hx-request": "true"}))
    require_csrf(_make_request({}))


def test_csrf_blocks_explicit_cross_site() -> None:
    with pytest.raises(CSRFError):
        require_csrf(_make_request({"sec-fetch-site": "cross-site"}))
