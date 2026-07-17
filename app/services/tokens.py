"""Ingest API tokens — scoped, opaque Bearer tokens for the Apple Shortcut.

An ingest token may submit ingestion jobs and nothing else (CONVENTIONS §6). The
ingest endpoint itself arrives in Phase 2; Phase 0 defines and enforces the scope
boundary so it is correct from the first ingest commit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.security import generate_token, hash_token, now_iso

INGEST_SCOPE = "ingest"
VALID_SCOPES = frozenset({INGEST_SCOPE})


@dataclass(frozen=True, slots=True)
class ApiToken:
    id: int
    user_id: int
    label: str
    scope: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


def _row(row: sqlite3.Row) -> ApiToken:
    return ApiToken(
        id=row["id"],
        user_id=row["user_id"],
        label=row["label"],
        scope=row["scope"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def create_ingest_token(conn: sqlite3.Connection, user_id: int, label: str) -> str:
    """Create an ingest-scoped token and return the raw value (shown once)."""
    raw = generate_token()
    conn.execute(
        "INSERT INTO api_tokens (token_hash, user_id, label, scope) VALUES (?, ?, ?, ?)",
        (hash_token(raw), user_id, label.strip() or "ingest token", INGEST_SCOPE),
    )
    conn.commit()
    return raw


def resolve_token(
    conn: sqlite3.Connection, raw_token: str, *, required_scope: str
) -> ApiToken | None:
    """Resolve a raw Bearer token, requiring an exact scope match.

    Returns None if unknown, revoked, or scoped for something else. Updates
    ``last_used_at`` on success.
    """
    if not raw_token or required_scope not in VALID_SCOPES:
        return None
    row = conn.execute(
        "SELECT * FROM api_tokens WHERE token_hash = ?",
        (hash_token(raw_token),),
    ).fetchone()
    if row is None:
        return None
    token = _row(row)
    if token.revoked_at is not None:
        return None
    if token.scope != required_scope:
        return None
    conn.execute(
        "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
        (now_iso(), token.id),
    )
    conn.commit()
    return token


def list_tokens(conn: sqlite3.Connection) -> list[ApiToken]:
    rows = conn.execute(
        "SELECT * FROM api_tokens ORDER BY revoked_at IS NOT NULL, created_at DESC"
    ).fetchall()
    return [_row(r) for r in rows]


def list_tokens_for_user(conn: sqlite3.Connection, user_id: int) -> list[ApiToken]:
    """A user's own active (non-revoked) ingest tokens, newest first."""
    rows = conn.execute(
        "SELECT * FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL "
        "ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def revoke_token(conn: sqlite3.Connection, token_id: int) -> bool:
    cur = conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now_iso(), token_id),
    )
    conn.commit()
    return cur.rowcount > 0
