"""Onboarding tokens — single-use magic links and typed pairing codes.

Both mint a new device session when consumed. A magic link carries a long opaque
token in a URL/QR; a pairing code is a short human-typed value shown on the admin
Devices page for cookie-less standalone launches (iOS PWA). Only hashes are stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.security import (
    ONBOARDING_LIFETIME,
    SESSION_LIFETIME,
    generate_pairing_code,
    generate_token,
    hash_token,
    now,
    now_iso,
    parse_iso,
    to_iso,
)


@dataclass(frozen=True, slots=True)
class IssuedOnboarding:
    kind: str
    raw: str  # the magic-link token or the pairing code, shown to the admin once
    device_name: str | None
    expires_at: str


@dataclass(frozen=True, slots=True)
class ConsumedOnboarding:
    user_id: int
    device_name: str | None


@dataclass(frozen=True, slots=True)
class BootstrappedAdmin:
    user_id: int
    session_token: str


def bootstrap_first_admin(
    conn: sqlite3.Connection, *, name: str, device_name: str
) -> BootstrappedAdmin | None:
    """Atomically create the first administrator and its initial device session."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("user name must not be empty")
    issued = now()
    raw = generate_token()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] != 0:
            conn.execute("ROLLBACK")
            return None
        cur = conn.execute(
            "INSERT INTO users (name, is_admin) VALUES (?, 1)",
            (clean_name,),
        )
        user_id = cur.lastrowid
        if user_id is None:  # pragma: no cover - SQLite always supplies a rowid
            raise RuntimeError("bootstrap user insert returned no rowid")
        conn.execute(
            """INSERT INTO device_sessions
               (token_hash, user_id, device_name, issued_at, last_seen_at, expires_at, renewed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                hash_token(raw),
                user_id,
                device_name.strip() or "device",
                to_iso(issued),
                to_iso(issued),
                to_iso(issued + SESSION_LIFETIME),
                to_iso(issued),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return BootstrappedAdmin(user_id=user_id, session_token=raw)


def _issue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    user_id: int,
    raw: str,
    device_name: str | None,
) -> IssuedOnboarding:
    expires = to_iso(now() + ONBOARDING_LIFETIME)
    conn.execute(
        """
        INSERT INTO onboarding_tokens (token_hash, kind, user_id, device_name, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (hash_token(raw), kind, user_id, device_name, expires),
    )
    conn.commit()
    return IssuedOnboarding(kind=kind, raw=raw, device_name=device_name, expires_at=expires)


def issue_magic_link(
    conn: sqlite3.Connection, user_id: int, device_name: str | None
) -> IssuedOnboarding:
    return _issue(
        conn, kind="magic_link", user_id=user_id, raw=generate_token(), device_name=device_name
    )


def issue_pairing_code(
    conn: sqlite3.Connection, user_id: int, device_name: str | None
) -> IssuedOnboarding:
    # Retry on the astronomically unlikely hash collision with a live code.
    for _ in range(10):
        code = generate_pairing_code()
        existing = conn.execute(
            "SELECT 1 FROM onboarding_tokens WHERE token_hash = ? AND used_at IS NULL",
            (hash_token(code),),
        ).fetchone()
        if existing is None:
            return _issue(
                conn, kind="pairing_code", user_id=user_id, raw=code, device_name=device_name
            )
    raise RuntimeError("could not generate a unique pairing code")  # pragma: no cover


def consume(conn: sqlite3.Connection, raw: str, *, kind: str) -> ConsumedOnboarding | None:
    """Validate and atomically consume a token of the given kind.

    Returns the associated user/device or None if the token is unknown, of the wrong
    kind, already used, or expired. Consumption is a single-row conditional update so
    it cannot be replayed.
    """
    raw = raw.strip()
    if kind == "pairing_code":
        raw = raw.upper()
    if not raw:
        return None
    row = conn.execute(
        "SELECT * FROM onboarding_tokens WHERE token_hash = ? AND kind = ?",
        (hash_token(raw), kind),
    ).fetchone()
    if row is None or row["used_at"] is not None:
        return None
    if parse_iso(row["expires_at"]) <= now():
        return None
    cur = conn.execute(
        "UPDATE onboarding_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (now_iso(), row["id"]),
    )
    conn.commit()
    if cur.rowcount != 1:  # lost a race — already consumed
        return None
    return ConsumedOnboarding(user_id=row["user_id"], device_name=row["device_name"])
