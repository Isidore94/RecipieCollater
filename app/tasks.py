"""Huey worker — durable background jobs on a SEPARATE queue database.

The queue lives in ``data/queue.db``, never the app DB, so queue bookkeeping writes
never contend with family-facing writes (CONVENTIONS §3). Run with:

    huey_consumer app.tasks.huey -w 1 -k thread

Phase 0 ships only worker plumbing (a liveness ``ping``, an hourly heartbeat, and the
nightly backup that exercises the Phase-0 backup framework). Heavy ingestion/AI tasks
arrive with their phases and will use a short-lived subprocess per job.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime

from huey import SqliteHuey, crontab

from app.config import get_settings
from app.logging_config import configure_logging, get_logger
from app.services import worker_health

_settings = get_settings()
_settings.ensure_dirs()

_immediate = os.environ.get("RC_HUEY_IMMEDIATE", "").strip().lower() in {"1", "true", "yes", "on"}

# The queue lives in its own SQLite file, never the app DB (CONVENTIONS §3).
QUEUE_DB_PATH = _settings.queue_db_path
huey = SqliteHuey(filename=str(QUEUE_DB_PATH), immediate=_immediate)

configure_logging(console=_settings.log_console)
log = get_logger("worker")
worker_health.write_heartbeat(_settings)


@huey.task()
def ping(value: str = "pong") -> str:
    """Liveness task used by tests and the admin worker check."""
    return value


@huey.task(retries=2, retry_delay=60)
def process_ingest_job(job_id: int, blocked_attempt: int = 0) -> None:
    """Process one ingest job (fetch -> extract -> save) in the worker subprocess.

    Expected failures (a blocked fetch, no recipe on the page) are recorded on the job by the
    pipeline and return normally. Only an *unexpected* error re-raises so Huey retries; the job is
    marked failed first so a stuck job never lingers mid-lifecycle.

    A YouTube "blocked" failure is expected but transient (it clears in hours, not the 60 s Huey
    retry gap), so it is re-scheduled on its own longer backoff via ``blocked_attempt`` instead.
    """
    from app.db import connect  # lazy heavy imports (CONVENTIONS 4)
    from app.services import ingest, pipeline

    conn = connect(_settings.db_path)
    try:
        job = ingest.get_job(conn, job_id)
        if job is None:
            log.warning("ingest_job_missing", job_id=job_id)
            return
        if blocked_attempt > 0 and not ingest.is_waiting_retry(job):
            # Stale scheduled retry: the job was done, retried by hand, or dismissed meanwhile.
            log.info("ingest_blocked_retry_stale", job_id=job_id, status=job.status)
            return
        ingest.increment_attempts(conn, job.id)
        pipeline.run_job(conn, job)
        final = ingest.get_job(conn, job_id)
        if final and final.status == "failed" and final.error_category == ingest.YOUTUBE_BLOCKED:
            plan = ingest.plan_blocked_retry(blocked_attempt, datetime.now(UTC))
            if plan is not None:
                ingest.mark_waiting_retry(conn, job_id, plan)
                process_ingest_job.schedule(
                    args=(job_id, plan.attempt), delay=plan.delay_seconds
                )
                log.info("ingest_blocked_retry_scheduled", job_id=job_id, attempt=plan.attempt)
                return
        log.info("ingest_job_processed", job_id=job_id, status=final.status if final else "gone")
    except Exception:
        log.exception("ingest_job_error", job_id=job_id)
        with contextlib.suppress(Exception):
            ingest.set_status(
                conn, job_id, "failed", error_category="worker_error",
                error_message="An unexpected error occurred while processing this link.",
            )
        raise
    finally:
        conn.close()


@huey.periodic_task(crontab(minute="0"))
def heartbeat() -> None:
    """Hourly worker heartbeat (near-zero cost); proves the consumer is alive."""
    log.info("worker_heartbeat")


@huey.periodic_task(crontab(minute="*"))
def record_worker_health() -> None:
    """Refresh the health marker; the web process treats markers older than 3 min as stale."""
    worker_health.write_heartbeat(_settings)


@huey.periodic_task(crontab(hour="3", minute="30"))
def nightly_backup() -> None:
    """Create a staged backup set nightly (framework exercised even with empty data)."""
    from app.services.backup import (  # lazy import (CONVENTIONS §4)
        backup_root,
        create_backup,
        prune_backups,
    )

    result = create_backup(_settings)
    removed = prune_backups(backup_root(_settings), keep=14)
    log.info(
        "nightly_backup",
        backup_dir=str(result.backup_dir),
        integrity_ok=result.integrity_ok,
        restore_tested=result.restore_tested,
        files=len(result.manifest["files"]),
        pruned=len(removed),
    )


@huey.periodic_task(crontab(day_of_week="0", hour="4", minute="15"))
def weekly_restore_test() -> None:
    """Weekly: restore the newest backup set into scratch and check it (CONVENTIONS §14).

    Runs after the nightly backup (Sundays 04:15). The result lands in ``restore-test.json``
    for the admin dashboard, which goes red if this stops running or fails.
    """
    from app.services.backup import run_restore_test  # lazy import (CONVENTIONS §4)

    result = run_restore_test(_settings)
    emit = log.info if result.ok else log.error
    emit(
        "weekly_restore_test",
        backup=result.backup_id,
        ok=result.ok,
        recipes=result.recipe_count,
        images=result.image_count,
        sampled=result.sampled_files,
        error=result.error,
    )
