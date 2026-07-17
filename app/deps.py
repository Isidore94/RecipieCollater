"""FastAPI dependencies: per-request SQLite connection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from app.config import get_settings
from app.db import connect


def get_db() -> Iterator[sqlite3.Connection]:
    """Open a per-request SQLite connection and close it afterwards.

    Per-request connections are simplest and correct under WAL for a family-scale LAN
    app: concurrent readers are fine and a single writer serialises naturally.
    """
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
