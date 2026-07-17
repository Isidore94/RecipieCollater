"""Backup framework and manifest format (architecture §7; roadmap Phase 0).

A backup is a *set*, not just a DB file: a ``VACUUM INTO`` snapshot of the app database
plus the ``images/`` and ``artifacts/`` trees, described by a checksum manifest. A backup
is only "healthy" after ``PRAGMA integrity_check`` passes and every manifest checksum
verifies (CONVENTIONS §14). Restore is implemented and smoke-tested so a backup that has
never been restored is not trusted.

Phase 0 establishes the framework and manifest even though images/artifacts are empty. Each set is
restored immediately and Phase 0 retains the newest 14; weekly retention is added in Phase 6.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app import __version__
from app.config import Settings
from app.security import now

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
_DB_SNAPSHOT_NAME = "recipecollater.db"


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_dir: Path
    manifest: dict[str, Any]
    integrity_ok: bool
    restore_tested: bool


class BackupDestinationError(RuntimeError):
    """The configured external backup destination is absent or unsafe."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity_check(db_path: Path) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        conn.close()


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _copy_tree(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.mkdir(parents=True, exist_ok=True)


def backup_root(settings: Settings) -> Path:
    """Where backup sets are written.

    Defaults to ``<data>/backups`` but should point at a DIFFERENT physical device
    (USB/NAS) in production via ``RC_BACKUP_DIR`` — a second partition on the same SSD
    does not cover disk death (architecture §7).
    """
    override = os.environ.get("RC_BACKUP_DIR")
    if not override:
        return settings.backups_dir
    root = Path(override).resolve()
    if not root.is_dir():
        raise BackupDestinationError(
            f"RC_BACKUP_DIR is not an existing directory (is the USB/NAS mounted?): {root}"
        )
    if not os.access(root, os.W_OK):
        raise BackupDestinationError(f"RC_BACKUP_DIR is not writable: {root}")
    try:
        if root.stat().st_dev == settings.data_dir.stat().st_dev:
            raise BackupDestinationError(
                "RC_BACKUP_DIR is on the same filesystem as RC_DATA_DIR; refusing an unsafe backup"
            )
    except FileNotFoundError as exc:
        raise BackupDestinationError(f"data or backup directory is missing: {exc}") from exc
    return root


def snapshot_database(source: Path, target: Path) -> None:
    """Create a transactionally consistent online copy using SQLite's backup API."""
    if not source.is_file():
        raise FileNotFoundError(f"source database not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"snapshot target already exists: {target}")
    src_conn = sqlite3.connect(source)
    dst_conn = sqlite3.connect(target)
    try:
        src_conn.backup(dst_conn)
    except Exception:
        dst_conn.close()
        target.unlink(missing_ok=True)
        raise
    else:
        dst_conn.close()
    finally:
        src_conn.close()


def create_backup(settings: Settings, dest_root: Path | None = None) -> BackupResult:
    root = dest_root or backup_root(settings)
    stamp = now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    backup_dir = root / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 1. Consistent online DB snapshot.
    snapshot = backup_dir / _DB_SNAPSHOT_NAME
    snapshot_database(settings.db_path, snapshot)

    # 2. Image and artifact trees (empty in Phase 0, but part of the contract).
    _copy_tree(settings.images_dir, backup_dir / "images")
    _copy_tree(settings.artifacts_dir, backup_dir / "artifacts")

    # 3. Manifest with per-file checksums.
    files: list[dict[str, Any]] = []
    for path in sorted(backup_dir.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            files.append(
                {
                    "path": path.relative_to(backup_dir).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                }
            )

    integrity_ok = _integrity_check(snapshot)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "app_version": __version__,
        "release_id": settings.release_id,
        "created_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "schema_version": _schema_version(snapshot),
        "db_snapshot": _DB_SNAPSHOT_NAME,
        "integrity_ok": integrity_ok,
        "restore_tested_at": None,
        "files": files,
    }
    (backup_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    restore_tested = smoke_test_restore(backup_dir)
    manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    return BackupResult(
        backup_dir=backup_dir,
        manifest=manifest,
        integrity_ok=integrity_ok,
        restore_tested=restore_tested,
    )


def verify_backup(backup_dir: Path) -> bool:
    """Re-verify a backup set: manifest present, checksums match, DB integrity ok."""
    try:
        manifest_path = backup_dir / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(path.is_symlink() for path in backup_dir.rglob("*")):
            return False
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            return False
        if manifest.get("integrity_ok") is not True or not isinstance(manifest.get("files"), list):
            return False

        listed: set[str] = set()
        for entry in manifest["files"]:
            relative = entry["path"]
            digest = entry["sha256"]
            pure = PurePosixPath(relative)
            if (
                not isinstance(relative, str)
                or pure.is_absolute()
                or ".." in pure.parts
                or relative in listed
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                return False
            listed.add(relative)
            path = backup_dir / relative
            if path.is_symlink() or not path.is_file() or _sha256_file(path) != digest:
                return False

        actual = {
            path.relative_to(backup_dir).as_posix()
            for path in backup_dir.rglob("*")
            if path.is_file() and path.name != MANIFEST_NAME
        }
        if listed != actual:
            return False

        db_name = manifest.get("db_snapshot")
        if db_name != _DB_SNAPSHOT_NAME or db_name not in listed:
            return False
        snapshot = backup_dir / db_name
        return _integrity_check(snapshot)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def backup_is_healthy(backup_dir: Path) -> bool:
    if not verify_backup(backup_dir):
        return False
    try:
        manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(manifest.get("restore_tested_at"))


def smoke_test_restore(backup_dir: Path) -> bool:
    """Restore into scratch, run DB integrity, then record that this set was restored."""
    with tempfile.TemporaryDirectory(prefix="recipecollater-restore-") as scratch:
        target = Path(scratch) / "data"
        restore_backup(backup_dir, target)
        if not _integrity_check(target / _DB_SNAPSHOT_NAME):
            return False
    manifest_path = backup_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["restore_tested_at"] = now().strftime("%Y-%m-%d %H:%M:%S")
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    return True


def restore_backup(backup_dir: Path, target_data_dir: Path) -> None:
    """Restore a verified backup set into a fresh data directory.

    Raises if the backup does not verify — we never restore an unverified backup.
    """
    if not verify_backup(backup_dir):
        raise ValueError(f"backup does not verify: {backup_dir}")
    if target_data_dir.exists() and any(target_data_dir.iterdir()):
        raise FileExistsError(f"restore target must be empty: {target_data_dir}")
    target_data_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    snapshot = backup_dir / manifest.get("db_snapshot", _DB_SNAPSHOT_NAME)
    shutil.copy2(snapshot, target_data_dir / "recipecollater.db")

    for tree in ("images", "artifacts"):
        src = backup_dir / tree
        dst = target_data_dir / tree
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            shutil.copytree(src, dst)
        else:
            dst.mkdir(parents=True, exist_ok=True)


def list_backups(root: Path) -> list[Path]:
    """List real backup *sets* (directories containing a manifest), newest name last.

    Only manifest-bearing directories count, so unrelated siblings (e.g. the migration
    runner's ``pre_migration/`` snapshot directory) are never treated as backups.
    """
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / MANIFEST_NAME).exists()),
        key=lambda p: p.name,
    )


def prune_backups(root: Path, keep: int = 14) -> list[Path]:
    """Keep the most recent ``keep`` backup sets; remove older ones. Returns removed dirs.

    Fail-safe: ``keep < 1`` is a no-op (never wipe every backup by accident). Phase 0 uses
    a simple most-recent-N policy; the 14-daily + 8-weekly schedule is layered on in Phase 6.
    """
    if keep < 1:
        return []
    backups = list_backups(root)
    removed: list[Path] = []
    for stale in backups[:-keep]:
        shutil.rmtree(stale)
        removed.append(stale)
    return removed
