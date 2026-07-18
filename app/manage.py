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
from app.db import connect, current_version, run_migrations
from app.logging_config import configure_logging, get_logger
from app.services import backup
from app.services import onboarding as onboarding_service
from app.services.users import list_users

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
    ok = backup.backup_is_healthy(result.backup_dir)
    log.info(
        "backup",
        dir=str(result.backup_dir),
        integrity_ok=result.integrity_ok,
        restore_tested=result.restore_tested,
        verified=ok,
    )
    # Deliberately last: deploy/update.sh records the exact set used for rollback.
    print(result.backup_dir)
    return 0 if ok else 1


def _cmd_snapshot_db(target: str) -> int:
    settings = get_settings()
    backup.snapshot_database(settings.db_path, Path(target))
    log.info("snapshot_db", source=str(settings.db_path), target=target)
    return 0


def _cmd_recover_admin(user_name: str | None, device_name: str) -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        admins = [user for user in list_users(conn) if user.is_admin]
        if user_name:
            admins = [user for user in admins if user.name.casefold() == user_name.casefold()]
        if len(admins) != 1:
            choices = ", ".join(user.name for user in admins) or "none"
            log.error("recover_admin_requires_one_match", matches=choices)
            return 2
        issued = onboarding_service.issue_pairing_code(conn, admins[0].id, device_name)
    finally:
        conn.close()
    print(f"Admin: {admins[0].name}")
    print(f"Pairing code (expires in 15 minutes): {issued.raw}")
    return 0


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


def _cmd_backfill_tags(all_recipes: bool, limit: int | None) -> int:
    """AI-tag recipes that predate the controlled tag vocabulary (Phase 4.6)."""
    from app.services import tagging  # heavy AI imports stay out of the module top level

    conn = connect(get_settings().db_path)
    try:
        results = tagging.backfill(conn, only_untagged=not all_recipes, limit=limit)
    finally:
        conn.close()
    failed = [r for r in results if r.error]
    for r in results:
        line = f"{r.title}: {', '.join(r.tags) if r.tags else '(no tags)'}"
        if r.error:
            line += f"  ERROR: {r.error}"
        print(line)
    log.info("backfill_tags", tagged=len(results) - len(failed), failed=len(failed))
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    configure_logging(console=get_settings().log_console)
    parser = argparse.ArgumentParser(prog="app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("backup")
    sub.add_parser("schema-version")
    p_snapshot = sub.add_parser("snapshot-db")
    p_snapshot.add_argument("target")
    p_recover = sub.add_parser("recover-admin")
    p_recover.add_argument("--user")
    p_recover.add_argument("--device-name", default="Recovered device")
    p_verify = sub.add_parser("verify-backup")
    p_verify.add_argument("backup_dir")
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("backup_dir")
    p_restore.add_argument("target")
    p_tags = sub.add_parser("backfill-tags")
    p_tags.add_argument("--all", action="store_true", help="retag even recipes that have tags")
    p_tags.add_argument("--limit", type=int, default=None)

    args = parser.parse_args(argv)
    match args.command:
        case "migrate":
            return _cmd_migrate()
        case "backup":
            return _cmd_backup()
        case "schema-version":
            return _cmd_schema_version()
        case "snapshot-db":
            return _cmd_snapshot_db(args.target)
        case "recover-admin":
            return _cmd_recover_admin(args.user, args.device_name)
        case "verify-backup":
            return _cmd_verify_backup(args.backup_dir)
        case "restore":
            return _cmd_restore(args.backup_dir, args.target)
        case "backfill-tags":
            return _cmd_backfill_tags(args.all, args.limit)
        case _:  # pragma: no cover - argparse (required=True) enforces valid commands
            parser.error(f"unknown command: {args.command}")  # NoReturn


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
