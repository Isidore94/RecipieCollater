"""Huey worker — durable background jobs on a SEPARATE queue database.

The queue lives in ``data/queue.db``, never the app DB, so queue bookkeeping writes
never contend with family-facing writes (CONVENTIONS §3). Run with:

    huey_consumer app.tasks.huey -w 1 -k thread

Phase 0 ships only worker plumbing (a liveness ``ping``, an hourly heartbeat, and the
nightly backup that exercises the Phase-0 backup framework). Heavy ingestion/AI tasks
arrive with their phases and will use a short-lived subprocess per job.
"""

from __future__ import annotations

import os

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
