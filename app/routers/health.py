"""Health check for monitoring and the staged-update temporary-port gate.

Unauthenticated by design: the staged updater curls it on a temp port before switching
the ``current`` symlink, and it must not depend on a paired device.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.db import connect, current_version, discover_migrations

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    settings = get_settings()
    checks: dict[str, str] = {}
    healthy = True

    # App database reachable + schema at the latest known migration.
    try:
        applied = current_version(settings.db_path)
        migrations = discover_migrations()
        latest = migrations[-1].version if migrations else 0
        checks["db"] = "ok"
        checks["schema_version"] = str(applied)
        if applied != latest:
            healthy = False
            checks["schema"] = f"behind (applied={applied}, latest={latest})"
        else:
            checks["schema"] = "current"
    except Exception as exc:
        healthy = False
        checks["db"] = f"error: {exc.__class__.__name__}"

    # Queue database reachable (separate file).
    try:
        qconn: sqlite3.Connection = connect(settings.queue_db_path)
        try:
            qconn.execute("SELECT 1")
        finally:
            qconn.close()
        checks["queue_db"] = "ok"
    except Exception as exc:
        healthy = False
        checks["queue_db"] = f"error: {exc.__class__.__name__}"

    body = {
        "status": "ok" if healthy else "unhealthy",
        "version": __version__,
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
