"""Backup framework: create → verify → tamper-detect → restore round trip.

A backup is trusted only after integrity check + checksum verification + a successful
restore (CONVENTIONS §14). The "empty backup/restore smoke test" is a Phase-0 exit
criterion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.db import connect, run_migrations
from app.services import backup
from app.services.users import create_user, list_users


def _make_data(data_dir: Path) -> None:
    db_path = data_dir / "recipecollater.db"
    run_migrations(db_path, backup_dir=data_dir / "backups" / "pre_migration")
    (data_dir / "images").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)


def test_empty_backup_restore_smoke(data_dir: Path) -> None:
    """Exit criterion: an empty backup verifies and restores even before recipe data."""
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)

    result = backup.create_backup(settings)
    assert result.integrity_ok is True
    assert result.restore_tested is True
    assert (result.backup_dir / "manifest.json").exists()
    assert backup.verify_backup(result.backup_dir) is True
    assert backup.backup_is_healthy(result.backup_dir) is True

    restore_target = data_dir.parent / "restored"
    backup.restore_backup(result.backup_dir, restore_target)
    assert (restore_target / "recipecollater.db").exists()


def test_backup_round_trip_preserves_rows(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)

    conn = connect(data_dir / "recipecollater.db")
    try:
        create_user(conn, "Aaron", is_admin=True)
        create_user(conn, "Sam")
    finally:
        conn.close()

    result = backup.create_backup(settings)

    restore_target = data_dir.parent / "restored"
    backup.restore_backup(result.backup_dir, restore_target)

    restored = connect(restore_target / "recipecollater.db")
    try:
        names = {u.name for u in list_users(restored)}
    finally:
        restored.close()
    assert names == {"Aaron", "Sam"}


def test_backup_covers_images_and_artifacts(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    (settings.images_dir / "1").mkdir(parents=True, exist_ok=True)
    (settings.images_dir / "1" / "hero.webp").write_bytes(b"not-really-webp")
    (settings.artifacts_dir / "9").mkdir(parents=True, exist_ok=True)
    (settings.artifacts_dir / "9" / "page.html.gz").write_bytes(b"gzip-bytes")

    result = backup.create_backup(settings)
    paths = {f["path"] for f in result.manifest["files"]}
    assert "images/1/hero.webp" in paths
    assert "artifacts/9/page.html.gz" in paths

    restore_target = data_dir.parent / "restored"
    backup.restore_backup(result.backup_dir, restore_target)
    assert (restore_target / "images" / "1" / "hero.webp").read_bytes() == b"not-really-webp"


def test_tampered_backup_fails_verify_and_restore(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    result = backup.create_backup(settings)

    # Corrupt a file after the manifest was written.
    snapshot = result.backup_dir / "recipecollater.db"
    snapshot.write_bytes(b"corrupted")
    assert backup.verify_backup(result.backup_dir) is False
    with pytest.raises(ValueError, match="does not verify"):
        backup.restore_backup(result.backup_dir, data_dir.parent / "restored")


def test_prune_keeps_most_recent(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    root = settings.backups_dir
    # Three real backup sets; the migration runner's pre_migration/ dir must be ignored.
    for _ in range(3):
        backup.create_backup(settings)
    ordered = backup.list_backups(root)
    assert len(ordered) == 3
    removed = backup.prune_backups(root, keep=2)
    assert removed == [ordered[0]]  # lowest-sorted set removed
    assert not ordered[0].exists()
    assert ordered[1].exists() and ordered[2].exists()
    assert (root / "pre_migration").exists()  # untouched by prune


def test_prune_keep_zero_is_noop(data_dir: Path) -> None:
    """Fail-safe: keep<1 must never wipe every backup."""
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    backup.create_backup(settings)
    removed = backup.prune_backups(settings.backups_dir, keep=0)
    assert removed == []
    assert len(backup.list_backups(settings.backups_dir)) == 1


def test_restore_refuses_nonempty_target(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    result = backup.create_backup(settings)
    target = data_dir.parent / "existing"
    target.mkdir()
    (target / "stale.db-wal").write_text("stale", encoding="utf-8")
    with pytest.raises(FileExistsError, match="must be empty"):
        backup.restore_backup(result.backup_dir, target)


def test_manifest_must_list_every_file(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    result = backup.create_backup(settings)
    (result.backup_dir / "unlisted.bin").write_bytes(b"not in manifest")
    assert backup.verify_backup(result.backup_dir) is False


def test_missing_external_backup_mount_fails(data_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    monkeypatch.setenv("RC_BACKUP_DIR", str(data_dir.parent / "missing-mount"))
    with pytest.raises(backup.BackupDestinationError, match="mounted"):
        backup.create_backup(settings)


def test_external_backup_must_be_another_filesystem(data_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    same_disk = data_dir.parent / "same-disk-backups"
    same_disk.mkdir()
    monkeypatch.setenv("RC_BACKUP_DIR", str(same_disk))
    with pytest.raises(backup.BackupDestinationError, match="same filesystem"):
        backup.create_backup(settings)


def test_backup_rejects_symlinks(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    result = backup.create_backup(settings)
    (result.backup_dir / "unexpected-link").symlink_to(result.backup_dir / "images")
    assert backup.verify_backup(result.backup_dir) is False
