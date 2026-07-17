"""User records — named household members (no passwords on the LAN)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    is_admin: bool
    created_at: str


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        is_admin=bool(row["is_admin"]),
        created_at=row["created_at"],
    )


def count_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def create_user(conn: sqlite3.Connection, name: str, *, is_admin: bool = False) -> User:
    name = name.strip()
    if not name:
        raise ValueError("user name must not be empty")
    cur = conn.execute(
        "INSERT INTO users (name, is_admin) VALUES (?, ?)",
        (name, 1 if is_admin else 0),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # a successful INSERT always yields a rowid
    user = get_user(conn, row_id)
    assert user is not None
    return user


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_name(conn: sqlite3.Connection, name: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE name = ?", (name.strip(),)).fetchone()
    return _row_to_user(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE").fetchall()
    return [_row_to_user(r) for r in rows]
