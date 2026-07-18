"""Admin dashboard stats (Phase 6): a single read-only health snapshot for the household admin.

Queue health, recent ingest failures, AI spend per provider, DB size, worker heartbeat, and the
last healthy backup - the things you want to glance at to know the box is fine (docs/08 Phase 6).
All cheap queries; no mutation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from app.ai import usage as ai_usage
from app.config import Settings
from app.services import backup, worker_health


@dataclass(frozen=True, slots=True)
class ProviderSpend:
    provider: str
    today_micros: int
    month_micros: int


@dataclass(frozen=True, slots=True)
class FailedJob:
    id: int
    url: str
    error_category: str | None
    error_message: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class DashboardStats:
    recipe_count: int
    cookbook_count: int
    pantry_item_count: int
    queue_pending: int
    queue_failed: int
    recent_failures: list[FailedJob] = field(default_factory=list)
    spend_today_micros: int = 0
    spend_month_micros: int = 0
    daily_cap_micros: int = 0
    monthly_cap_micros: int = 0
    provider_spend: list[ProviderSpend] = field(default_factory=list)
    db_bytes: int = 0
    schema_version: int = 0
    worker_age_seconds: float | None = None
    worker_ok: bool = False
    last_backup: str | None = None
    last_backup_healthy: bool = False
    ytdlp_version: str | None = None


_ACTIVE = ("queued", "fetching", "extracting", "normalizing")


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _ytdlp_version() -> str | None:
    try:
        from yt_dlp import version as ytv  # lazy: heavy (CONVENTIONS 4)

        return str(ytv.__version__)
    except Exception:
        return None


def gather(conn: sqlite3.Connection, settings: Settings) -> DashboardStats:
    # _ACTIVE is a fixed module tuple, never user input; the placeholders are ours.
    placeholders = ",".join("?" for _ in _ACTIVE)
    pending = _scalar(
        conn,
        f"SELECT COUNT(*) FROM ingest_jobs WHERE status IN ({placeholders})",  # noqa: S608
        *_ACTIVE,
    )
    failed = _scalar(conn, "SELECT COUNT(*) FROM ingest_jobs WHERE status = 'failed'")
    failures = [
        FailedJob(
            id=int(r["id"]), url=r["url"], error_category=r["error_category"],
            error_message=r["error_message"], updated_at=r["updated_at"],
        )
        for r in conn.execute(
            "SELECT id, url, error_category, error_message, updated_at FROM ingest_jobs "
            "WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 8"
        ).fetchall()
    ]
    provider_rows = conn.execute(
        """SELECT provider,
                  COALESCE(SUM(CASE WHEN created_at >= date('now')
                                    THEN cost_micros END), 0) AS today,
                  COALESCE(SUM(CASE WHEN created_at >= date('now','start of month')
                                    THEN cost_micros END), 0) AS month
           FROM ai_usage_log GROUP BY provider ORDER BY provider"""
    ).fetchall()
    provider_spend = [
        ProviderSpend(provider=r["provider"], today_micros=int(r["today"]),
                      month_micros=int(r["month"]))
        for r in provider_rows
    ]

    heartbeat = worker_health.read_heartbeat(settings)
    db_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    schema_version = _scalar(
        conn, "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    )

    last_backup: str | None = None
    last_backup_healthy = False
    backups = backup.list_backups(backup.backup_root(settings))
    if backups:
        newest = backups[-1]
        last_backup = newest.name
        last_backup_healthy = backup.backup_is_healthy(newest)

    return DashboardStats(
        recipe_count=_scalar(conn, "SELECT COUNT(*) FROM recipes"),
        cookbook_count=_scalar(conn, "SELECT COUNT(*) FROM recipes WHERE status = 'cookbook'"),
        pantry_item_count=_scalar(conn, "SELECT COUNT(*) FROM pantry_items"),
        queue_pending=pending,
        queue_failed=failed,
        recent_failures=failures,
        spend_today_micros=ai_usage.spend_today_micros(conn),
        spend_month_micros=ai_usage.spend_month_micros(conn),
        daily_cap_micros=settings.ai_daily_cap_micros,
        monthly_cap_micros=settings.ai_monthly_cap_micros,
        provider_spend=provider_spend,
        db_bytes=db_bytes,
        schema_version=schema_version,
        worker_age_seconds=heartbeat.age_seconds if heartbeat else None,
        worker_ok=bool(heartbeat and heartbeat.age_seconds < worker_health.MAX_HEARTBEAT_AGE
                       .total_seconds()),
        last_backup=last_backup,
        last_backup_healthy=last_backup_healthy,
        ytdlp_version=_ytdlp_version(),
    )
