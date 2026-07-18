"""Migration runner: fresh install, ordering, atomic failure/rollback, snapshots,
upgrade-from-prior-snapshot, and tamper detection (roadmap Phase 0 verification)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db import (
    MIGRATIONS_DIR,
    MigrationError,
    connect,
    current_version,
    discover_migrations,
    run_migrations,
)


def _write(migrations: Path, name: str, sql: str) -> None:
    migrations.mkdir(parents=True, exist_ok=True)
    (migrations / name).write_text(sql, encoding="utf-8")


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


# ---- The real Phase-0 migration -------------------------------------------------------


def test_real_migration_001_is_only_phase0_tables(tmp_path: Path) -> None:
    """Migration 001 must contain ONLY Phase-0 tables (no speculative later-phase tables)."""
    only_first = tmp_path / "m"
    only_first.mkdir()
    shutil.copy2(MIGRATIONS_DIR / "001_phase0_identity.sql", only_first)
    db_path = tmp_path / "app.db"
    run_migrations(db_path, only_first, backup_dir=tmp_path / "bk")
    conn = connect(db_path)
    try:
        tables = _tables(conn)
    finally:
        conn.close()
    phase0 = {"users", "device_sessions", "api_tokens", "onboarding_tokens"}
    assert phase0 <= tables
    forbidden = {
        "recipes",
        "recipe_ingredients",
        "pantry_items",
        "shopping_lists",
        "ingest_jobs",
        "meal_plan_entries",
        "ai_usage_log",
        "settings",
        "foods",
        "units",
    }
    leaked = forbidden & tables
    assert not leaked, f"Phase-0 must not create later-phase tables: {leaked}"


def test_fresh_install_and_idempotent_rerun(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    first = run_migrations(db_path, backup_dir=tmp_path / "bk")
    assert first.applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert first.current_version == 14
    second = run_migrations(db_path, backup_dir=tmp_path / "bk")
    assert second.already_current is True
    assert second.applied == []
    assert current_version(db_path) == 14


def test_snapshot_taken_before_each_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup = tmp_path / "bk"
    result = run_migrations(db_path, backup_dir=backup)
    assert result.snapshots
    for snap in result.snapshots:
        assert snap.exists()
        # Each snapshot is a valid SQLite database.
        c = sqlite3.connect(snap)
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.close()


# ---- Ordering / discovery validation --------------------------------------------------


def test_gap_in_versions_rejected(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "001_a.sql", "CREATE TABLE a (id INTEGER);")
    _write(m, "003_c.sql", "CREATE TABLE c (id INTEGER);")
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(m)


def test_duplicate_version_rejected(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "001_a.sql", "CREATE TABLE a (id INTEGER);")
    _write(m, "001_b.sql", "CREATE TABLE b (id INTEGER);")
    with pytest.raises(MigrationError, match="duplicate"):
        discover_migrations(m)


def test_bad_filename_rejected(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "init.sql", "CREATE TABLE a (id INTEGER);")
    with pytest.raises(MigrationError, match="NNN_snake_case"):
        discover_migrations(m)


def test_non_sql_file_rejected(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "001_a.sql", "CREATE TABLE a (id INTEGER);")
    (m / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(MigrationError, match="non-.sql"):
        discover_migrations(m)


# ---- Atomic failure / rollback --------------------------------------------------------


def test_failed_migration_rolls_back_and_records_nothing(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "001_ok.sql", "CREATE TABLE keep (id INTEGER);")
    db_path = tmp_path / "app.db"
    run_migrations(db_path, m, backup_dir=tmp_path / "bk")  # applies 001 cleanly

    # Now add a later migration whose second statement is invalid.
    _write(m, "002_bad.sql", "CREATE TABLE t2 (id INTEGER);\nCREATE TABLE t2 (id INTEGER);")
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, m, backup_dir=tmp_path / "bk")  # 002 fails atomically

    conn = connect(db_path)
    try:
        assert current_version(db_path) == 1  # still at 001
        tables = _tables(conn)
        assert "keep" in tables
        assert "t2" not in tables  # first statement of the failed migration was rolled back
    finally:
        conn.close()


def test_partial_migration_leaves_no_tables(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(
        m,
        "001_partial.sql",
        "CREATE TABLE first (id INTEGER);\nCREATE TABLE first (id INTEGER);",  # dup → fails
    )
    db_path = tmp_path / "app.db"
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db_path, m, backup_dir=tmp_path / "bk")
    conn = connect(db_path)
    try:
        assert "first" not in _tables(conn)
        assert current_version(db_path) == 0
    finally:
        conn.close()


# ---- Upgrade from a prior released snapshot -------------------------------------------


def test_upgrade_from_prior_snapshot_preserves_data(tmp_path: Path) -> None:
    """Simulate the staged-update rehearsal: migrate a copy of the previous release's DB."""
    m = tmp_path / "migrations"
    _write(m, "001_users.sql", "CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT);")
    db_path = tmp_path / "app.db"
    run_migrations(db_path, m, backup_dir=tmp_path / "bk")

    conn = connect(db_path)
    conn.execute("INSERT INTO person (name) VALUES ('Aaron')")
    conn.commit()
    conn.close()

    # "Previous released snapshot": copy the DB aside.
    snapshot = tmp_path / "release_snapshot.db"
    shutil.copy2(db_path, snapshot)

    # New release adds migration 002.
    _write(m, "002_add_col.sql", "ALTER TABLE person ADD COLUMN nickname TEXT;")

    # Rehearse the upgrade against the copied snapshot (not the live DB).
    result = run_migrations(snapshot, m, backup_dir=tmp_path / "bk2")
    assert result.applied == [2]

    conn = connect(snapshot)
    try:
        row = conn.execute("SELECT name FROM person").fetchone()
        assert row["name"] == "Aaron"  # data preserved across the upgrade
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(person)").fetchall()}
        assert "nickname" in cols
    finally:
        conn.close()


def test_real_upgrade_from_phase0_001_to_002(tmp_path: Path) -> None:
    """A database created by the original Phase-0 commit upgrades without editing 001."""
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    shutil.copy2(MIGRATIONS_DIR / "001_phase0_identity.sql", old_migrations)
    db_path = tmp_path / "old-release.db"
    run_migrations(db_path, old_migrations, backup_dir=tmp_path / "old-backups")
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO users (name, is_admin) VALUES ('Aaron', 1)")
        conn.commit()
    finally:
        conn.close()

    result = run_migrations(db_path, backup_dir=tmp_path / "new-backups")
    assert result.applied == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    conn = connect(db_path)
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(device_sessions)").fetchall()
        }
        assert "renewed_at" in columns
        assert conn.execute("SELECT name FROM users").fetchone()["name"] == "Aaron"
    finally:
        conn.close()


# ---- Integrity / tamper detection -----------------------------------------------------


def test_checksum_change_after_apply_is_rejected(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "001_a.sql", "CREATE TABLE a (id INTEGER);")
    db_path = tmp_path / "app.db"
    run_migrations(db_path, m, backup_dir=tmp_path / "bk")

    # Tamper with an already-applied migration file.
    _write(m, "001_a.sql", "CREATE TABLE a (id INTEGER, extra TEXT);")
    with pytest.raises(MigrationError, match="checksum mismatch"):
        run_migrations(db_path, m, backup_dir=tmp_path / "bk")


def test_database_ahead_of_code_is_rejected(tmp_path: Path) -> None:
    m = tmp_path / "migrations"
    _write(m, "001_a.sql", "CREATE TABLE a (id INTEGER);")
    _write(m, "002_b.sql", "CREATE TABLE b (id INTEGER);")
    db_path = tmp_path / "app.db"
    run_migrations(db_path, m, backup_dir=tmp_path / "bk")

    # Code "rolls back" to only migration 001, but DB already has 002 applied.
    (m / "002_b.sql").unlink()
    with pytest.raises(MigrationError, match="ahead of the code"):
        run_migrations(db_path, m, backup_dir=tmp_path / "bk")


def test_pragmas_applied_on_connect(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    run_migrations(db_path, backup_dir=tmp_path / "bk")
    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_real_migrations_discoverable() -> None:
    """The shipped migrations directory validates and starts at 001."""
    migrations = discover_migrations()
    assert migrations
    assert migrations[0].version == 1
