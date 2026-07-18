"""The management CLI used by the deploy scripts (migrate/backup/verify/restore)."""

from __future__ import annotations

from pathlib import Path

from app import config
from app.db import connect
from app.manage import main
from app.services.users import create_user, list_users


def test_migrate_and_schema_version(data_dir: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["migrate"]) == 0
    assert main(["schema-version"]) == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out == "13"


def test_backup_verify_restore_cycle(data_dir: Path) -> None:
    assert main(["migrate"]) == 0
    conn = connect(config.get_settings().db_path)
    try:
        create_user(conn, "Aaron", is_admin=True)
    finally:
        conn.close()

    assert main(["backup"]) == 0
    backups = sorted((config.get_settings().backups_dir).glob("*/manifest.json"))
    assert backups
    backup_dir = backups[-1].parent

    assert main(["verify-backup", str(backup_dir)]) == 0

    target = data_dir.parent / "restored"
    assert main(["restore", str(backup_dir), str(target)]) == 0

    restored = connect(target / "recipecollater.db")
    try:
        assert {u.name for u in list_users(restored)} == {"Aaron"}
    finally:
        restored.close()


def test_snapshot_db_and_admin_recovery(data_dir: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["migrate"]) == 0
    conn = connect(config.get_settings().db_path)
    try:
        create_user(conn, "Aaron", is_admin=True)
    finally:
        conn.close()

    snapshot = data_dir.parent / "rehearsal.db"
    assert main(["snapshot-db", str(snapshot)]) == 0
    copied = connect(snapshot)
    try:
        assert {u.name for u in list_users(copied)} == {"Aaron"}
    finally:
        copied.close()

    assert main(["recover-admin", "--user", "aaron", "--device-name", "New PC"]) == 0
    output = capsys.readouterr().out
    assert "Pairing code" in output
