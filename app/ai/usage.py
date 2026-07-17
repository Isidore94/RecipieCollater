"""AI spend accounting: log every call and enforce integer micro-USD daily/monthly caps.

Spend windows are computed with SQLite's own clock (date('now')) so they always agree with the
created_at the rows were written with - no app-vs-DB timezone drift.
"""

from __future__ import annotations

import sqlite3

from app.config import Settings


def spend_today_micros(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_micros), 0) AS spent FROM ai_usage_log "
        "WHERE created_at >= date('now')"
    ).fetchone()
    return int(row["spent"])


def spend_month_micros(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_micros), 0) AS spent FROM ai_usage_log "
        "WHERE created_at >= date('now', 'start of month')"
    ).fetchone()
    return int(row["spent"])


def within_budget(conn: sqlite3.Connection, settings: Settings) -> bool:
    """True while accumulated spend is under both the daily and monthly cap.

    This is a soft cap: it blocks the *next* call once spend crosses the threshold, so a single
    in-flight call can overshoot by at most its own cost. Because billed-but-failed calls are also
    counted (AIError carries their cost), the overshoot stays bounded and can't run away.
    """
    if spend_today_micros(conn) >= settings.ai_daily_cap_micros:
        return False
    return spend_month_micros(conn) < settings.ai_monthly_cap_micros


def log_usage(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    operation: str,
    job_id: int | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int = 0,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO ai_usage_log
           (provider, model, operation, job_id, input_tokens, output_tokens, cost_micros,
            status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (provider, model, operation, job_id, input_tokens, output_tokens, cost_micros, status,
         error),
    )
    conn.commit()
