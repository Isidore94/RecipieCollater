"""Backup framework and manifest format (architecture §7; roadmap Phase 0).

A backup is a *set*, not just a DB file: a ``VACUUM INTO`` snapshot of the app database
plus the ``images/`` and ``artifacts/`` trees, described by a checksum manifest. A backup
is only "healthy" after ``PRAGMA integrity_check`` passes and every manifest checksum
verifies (CONVENTIONS §14). Restore is implemented and smoke-tested so a backup that has
never been restored is not trusted.

Phase 0 establishes the framework and manifest even though images/artifacts are empty;
rotation policy (14 daily + 8 weekly) and the scheduled restore smoke test are wired here
and refined in Phase 6.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import __version__
from app.config import Settings
from app.db import current_version
from app.security import now

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
_DB_SNAPSHOT_NAME = "recipecollater.db"


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_dir: Path
    manifest: dict[str, Any]
    integrity_ok: bool


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
    return Path(override).resolve() if override else settings.backups_dir


def create_backup(settings: Settings, dest_root: Path | None = None) -> BackupResult:
    root = dest_root or backup_root(settings)
    stamp = now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    backup_dir = root / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 1. Consistent online DB snapshot.
    snapshot = backup_dir / _DB_SNAPSHOT_NAME
    src_conn = sqlite3.connect(settings.db_path, isolation_level=None)
    try:
        src_conn.execute("VACUUM INTO ?", (str(snapshot),))
    finally:
        src_conn.close()

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
        "created_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "schema_version": current_version(settings.db_path),
        "db_snapshot": _DB_SNAPSHOT_NAME,
        "integrity_ok": integrity_ok,
        "files": files,
    }
    (backup_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return BackupResult(backup_dir=backup_dir, manifest=manifest, integrity_ok=integrity_ok)


def verify_backup(backup_dir: Path) -> bool:
    """Re-verify a backup set: manifest present, checksums match, DB integrity ok."""
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for entry in manifest.get("files", []):
        path = backup_dir / entry["path"]
        if not path.exists() or _sha256_file(path) != entry["sha256"]:
            return False

    snapshot = backup_dir / manifest.get("db_snapshot", _DB_SNAPSHOT_NAME)
    if not snapshot.exists():
        return False
    return _integrity_check(snapshot)


def restore_backup(backup_dir: Path, target_data_dir: Path) -> None:
    """Restore a verified backup set into a fresh data directory.

    Raises if the backup does not verify — we never restore an unverified backup.
    """
    if not verify_backup(backup_dir):
        raise ValueError(f"backup does not verify: {backup_dir}")
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
