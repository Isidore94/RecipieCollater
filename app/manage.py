"""Management CLI used by the deploy scripts and for local operations.

    python -m app.manage migrate                 # migrate the configured data DB
    python -m app.manage backup                  # create a verified backup set
    python -m app.manage verify-backup <dir>     # re-verify an existing backup set
    python -m app.manage restore <dir> <target>  # restore a backup into a data dir
    python -m app.manage schema-version          # print the applied schema version

All commands honour RC_DATA_DIR (and RC_BACKUP_DIR for backups), so the staged updater
can migrate a *copy* of the database by pointing RC_DATA_DIR at the copy directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.db import current_version, run_migrations
from app.logging_config import configure_logging, get_logger
from app.services import backup

log = get_logger("manage")


def _cmd_migrate() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    result = run_migrations(settings.db_path, backup_dir=settings.backups_dir / "pre_migration")
    log.info(
        "migrate",
        current_version=result.current_version,
        applied=result.applied,
        already_current=result.already_current,
    )
    return 0


def _cmd_backup() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    result = backup.create_backup(settings)
    ok = backup.verify_backup(result.backup_dir)
    log.info("backup", dir=str(result.backup_dir), integrity_ok=result.integrity_ok, verified=ok)
    return 0 if ok else 1


def _cmd_verify_backup(backup_dir: str) -> int:
    ok = backup.verify_backup(Path(backup_dir))
    log.info("verify_backup", dir=backup_dir, verified=ok)
    return 0 if ok else 1


def _cmd_restore(backup_dir: str, target: str) -> int:
    backup.restore_backup(Path(backup_dir), Path(target))
    log.info("restore", dir=backup_dir, target=target)
    return 0


def _cmd_schema_version() -> int:
    print(current_version(get_settings().db_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging(console=get_settings().log_console)
    parser = argparse.ArgumentParser(prog="app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("backup")
    sub.add_parser("schema-version")
    p_verify = sub.add_parser("verify-backup")
    p_verify.add_argument("backup_dir")
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("backup_dir")
    p_restore.add_argument("target")

    args = parser.parse_args(argv)
    match args.command:
        case "migrate":
            return _cmd_migrate()
        case "backup":
            return _cmd_backup()
        case "schema-version":
            return _cmd_schema_version()
        case "verify-backup":
            return _cmd_verify_backup(args.backup_dir)
        case "restore":
            return _cmd_restore(args.backup_dir, args.target)
        case _:  # pragma: no cover - argparse (required=True) enforces valid commands
            parser.error(f"unknown command: {args.command}")  # NoReturn


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
