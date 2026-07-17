"""Device sessions — the one persistent browser cookie's authority (CONVENTIONS §5).

The cookie carries an opaque token; the DB stores only its hash. Resolution checks
expiry and revocation, updates ``last_seen_at``, and signals sliding renewal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.security import (
    SESSION_LIFETIME,
    SESSION_RENEW_AFTER,
    generate_token,
    hash_token,
    now,
    now_iso,
    parse_iso,
    to_iso,
)


@dataclass(frozen=True, slots=True)
class DeviceSession:
    id: int
    user_id: int
    device_name: str
    issued_at: str
    last_seen_at: str | None
    expires_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    session: DeviceSession
    renewed: bool  # caller should re-set the cookie to extend Max-Age


def _row(row: sqlite3.Row) -> DeviceSession:
    return DeviceSession(
        id=row["id"],
        user_id=row["user_id"],
        device_name=row["device_name"],
        issued_at=row["issued_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def create_session(conn: sqlite3.Connection, user_id: int, device_name: str) -> str:
    """Create a session row and return the raw token (shown to the client once)."""
    raw = generate_token()
    issued = now()
    expires = issued + SESSION_LIFETIME
    conn.execute(
        """
        INSERT INTO device_sessions
            (token_hash, user_id, device_name, issued_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            hash_token(raw),
            user_id,
            device_name.strip() or "device",
            to_iso(issued),
            to_iso(issued),
            to_iso(expires),
        ),
    )
    conn.commit()
    return raw


def resolve_session(conn: sqlite3.Connection, raw_token: str) -> ResolvedSession | None:
    """Return the live session for a raw cookie token, or None if invalid.

    Applies sliding renewal: bumps ``expires_at`` and flags the caller to re-set the
    cookie when the session is older than the renewal threshold.
    """
    if not raw_token:
        return None
    row = conn.execute(
        "SELECT * FROM device_sessions WHERE token_hash = ?",
        (hash_token(raw_token),),
    ).fetchone()
    if row is None:
        return None
    session = _row(row)
    if session.revoked_at is not None:
        return None

    current = now()
    if parse_iso(session.expires_at) <= current:
        return None

    renewed = False
    new_expires = session.expires_at
    if current - parse_iso(session.issued_at) > SESSION_RENEW_AFTER:
        renewed = True
        new_expires = to_iso(current + SESSION_LIFETIME)
        conn.execute(
            "UPDATE device_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (now_iso(), new_expires, session.id),
        )
    else:
        conn.execute(
            "UPDATE device_sessions SET last_seen_at = ? WHERE id = ?",
            (now_iso(), session.id),
        )
    conn.commit()

    refreshed = DeviceSession(
        id=session.id,
        user_id=session.user_id,
        device_name=session.device_name,
        issued_at=session.issued_at,
        last_seen_at=now_iso(),
        expires_at=new_expires,
        revoked_at=None,
    )
    return ResolvedSession(session=refreshed, renewed=renewed)


def list_sessions(conn: sqlite3.Connection) -> list[DeviceSession]:
    rows = conn.execute(
        "SELECT * FROM device_sessions ORDER BY revoked_at IS NOT NULL, last_seen_at DESC"
    ).fetchall()
    return [_row(r) for r in rows]


def revoke_session(conn: sqlite3.Connection, session_id: int) -> bool:
    cur = conn.execute(
        "UPDATE device_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now_iso(), session_id),
    )
    conn.commit()
    return cur.rowcount > 0
