"""Backup framework: create → verify → tamper-detect → restore round trip.

A backup is trusted only after integrity check + checksum verification + a successful
restore (CONVENTIONS §14). The "empty backup/restore smoke test" is a Phase-0 exit
criterion.
"""

from __future__ import annotations

import json
from dataclasses import asdict
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


def _symlinks_supported() -> bool:
    # Windows needs admin/Developer Mode to create symlinks; skip there rather than fail.
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "t"
            target.write_text("x", encoding="utf-8")
            (Path(scratch) / "l").symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.mark.skipif(
    not _symlinks_supported(), reason="symlink creation requires admin/Developer Mode on Windows"
)
def test_backup_rejects_symlinks(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    result = backup.create_backup(settings)
    (result.backup_dir / "unexpected-link").symlink_to(result.backup_dir / "images")
    assert backup.verify_backup(result.backup_dir) is False


# ---- restore smoke test (weekly) -------------------------------------------------------


def _backup_with_images(data_dir: Path, n_images: int = 3) -> backup.BackupResult:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    for i in range(n_images):
        (settings.images_dir / str(i)).mkdir(parents=True, exist_ok=True)
        (settings.images_dir / str(i) / "hero.webp").write_bytes(f"img-{i}".encode())
    return backup.create_backup(settings)


def test_manifest_records_recipe_and_image_counts(data_dir: Path) -> None:
    result = _backup_with_images(data_dir, 3)
    assert result.manifest["recipe_count"] == 0
    assert result.manifest["image_count"] == 3


def test_restore_test_passes_and_writes_json(data_dir: Path) -> None:
    result = _backup_with_images(data_dir, 3)
    settings = config.get_settings()

    outcome = backup.run_restore_test(settings)

    assert outcome.ok is True and outcome.error is None
    assert outcome.backup_id == result.backup_dir.name
    assert (outcome.recipe_count, outcome.manifest_recipe_count) == (0, 0)
    assert (outcome.image_count, outcome.manifest_image_count) == (3, 3)
    assert outcome.sampled_files == 3
    saved = backup.read_restore_test(settings)
    assert saved == outcome
    assert json.loads(settings.restore_test_path.read_text())["ok"] is True
    # scratch dir is gone, the data dir holds no leftovers
    assert not [p for p in data_dir.iterdir() if p.name.startswith(".restore-test-")]


def test_restore_test_counts_real_recipes(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO recipes (title, slug, base_servings) "
            "VALUES ('A', 'a', '4'), ('B', 'b', '4')"
        )
        conn.commit()
    finally:
        conn.close()
    backup.create_backup(settings)

    outcome = backup.run_restore_test(settings)
    assert outcome.ok is True
    assert (outcome.recipe_count, outcome.manifest_recipe_count) == (2, 2)


def test_restore_test_samples_a_bounded_number_of_images(data_dir: Path) -> None:
    _backup_with_images(data_dir, 6)
    outcome = backup.run_restore_test(config.get_settings(), sample=2)
    assert outcome.ok is True
    assert outcome.sampled_files == 2


def test_restore_test_fails_on_tampered_backup_and_records_it(data_dir: Path) -> None:
    result = _backup_with_images(data_dir, 2)
    (result.backup_dir / "images" / "0" / "hero.webp").write_bytes(b"flipped")
    settings = config.get_settings()

    outcome = backup.run_restore_test(settings)

    assert outcome.ok is False
    assert outcome.error is not None and "does not verify" in outcome.error
    saved = backup.read_restore_test(settings)
    assert saved is not None and saved.ok is False
    assert not [p for p in data_dir.iterdir() if p.name.startswith(".restore-test-")]


def test_restore_test_detects_count_mismatch_with_manifest(data_dir: Path) -> None:
    """A manifest that promises more recipes than the restored DB holds is a failure."""
    result = _backup_with_images(data_dir, 1)
    manifest_path = result.backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["recipe_count"] = 5
    manifest_path.write_text(json.dumps(manifest))

    outcome = backup.run_restore_test(config.get_settings(), result.backup_dir)
    assert outcome.ok is False
    assert outcome.error is not None and "recipe count 0 does not match" in outcome.error


def test_restore_test_accepts_older_manifest_without_counts(data_dir: Path) -> None:
    result = _backup_with_images(data_dir, 1)
    manifest_path = result.backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["recipe_count"], manifest["image_count"]
    manifest_path.write_text(json.dumps(manifest))

    outcome = backup.run_restore_test(config.get_settings(), result.backup_dir)
    assert outcome.ok is True
    assert outcome.manifest_recipe_count is None


def test_restore_test_with_no_backups_fails_cleanly(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    outcome = backup.run_restore_test(settings)
    assert outcome.ok is False and outcome.backup_id is None
    assert outcome.error is not None and "no backup sets" in outcome.error


def test_read_restore_test_tolerates_missing_and_garbage(data_dir: Path) -> None:
    settings = config.get_settings()
    settings.ensure_dirs()
    assert backup.read_restore_test(settings) is None
    settings.restore_test_path.write_text("{not json")
    assert backup.read_restore_test(settings) is None
    settings.restore_test_path.write_text('{"unexpected": 1}')
    assert backup.read_restore_test(settings) is None


def test_dashboard_restore_test_alerting(data_dir: Path) -> None:
    from datetime import timedelta

    from app.security import now, to_iso
    from app.services import admin_stats

    settings = config.get_settings()
    settings.ensure_dirs()
    _make_data(data_dir)
    conn = connect(settings.db_path)
    try:
        # never run -> red
        stats = admin_stats.gather(conn, settings)
        assert stats.restore_test_at is None and stats.restore_test_alert is True

        def _write(age_days: int, ok: bool) -> None:
            result = backup.RestoreTestResult(
                tested_at=to_iso(now() - timedelta(days=age_days)), backup_id="b", ok=ok,
                error=None if ok else "boom", recipe_count=0, manifest_recipe_count=0,
                image_count=0, manifest_image_count=0, sampled_files=0,
            )
            settings.restore_test_path.write_text(json.dumps(asdict(result)))

        _write(1, True)  # fresh and OK -> not red
        stats = admin_stats.gather(conn, settings)
        assert stats.restore_test_ok and not stats.restore_test_alert
        assert stats.restore_test_age_days == 1

        _write(8, True)  # older than 7 days -> red
        assert admin_stats.gather(conn, settings).restore_test_alert is True

        _write(0, False)  # fresh but failed -> red
        stats = admin_stats.gather(conn, settings)
        assert stats.restore_test_alert is True and stats.restore_test_error == "boom"
    finally:
        conn.close()
