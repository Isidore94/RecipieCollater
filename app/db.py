"""SQLite connection factory and the ordered, forward-only migration runner.

No ORM (CONVENTIONS §2). Migrations are plain SQL files applied in order inside a
transaction, tracked in `schema_migrations`, with a `VACUUM INTO` snapshot taken
before every apply (CONVENTIONS §13).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import PACKAGE_DIR
from app.logging_config import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
_MIGRATION_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# Pragmas applied to every connection (docs/03-data-model.md §1). journal_mode=WAL
# persists in the file header but is harmless to re-assert; foreign_keys is per-connection.
_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("busy_timeout", "5000"),
    ("synchronous", "NORMAL"),
    ("cache_size", "-64000"),  # 64 MB
    ("foreign_keys", "ON"),
)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the project pragmas and a Row factory.

    isolation_level="" keeps Python's implicit transaction handling for normal DML;
    the migration runner manages its own explicit transactions.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Default isolation_level ("") keeps Python's implicit transaction handling for normal
    # DML; the migration runner manages its own explicit BEGIN/COMMIT.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for name, value in _PRAGMAS:
        conn.execute(f"PRAGMA {name}={value}")
    return conn


# --------------------------------------------------------------------------------------
# SQL statement splitting (trigger-aware so future FTS5 trigger migrations apply atomically)
# --------------------------------------------------------------------------------------


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    Handles line/block comments, single- and double-quoted literals, and compound
    ``BEGIN ... END`` blocks (triggers) so semicolons inside a trigger body do not
    split a statement. Migration files must not contain ``BEGIN``/``COMMIT``/``VACUUM``
    transaction control — the runner owns transactions (CONVENTIONS §13).
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    block_depth = 0  # nesting of BEGIN...END compound blocks

    def _word_at(idx: int) -> str | None:
        m = re.match(r"[A-Za-z_]+", sql[idx:])
        return m.group(0) if m else None

    while i < n:
        ch = sql[i]

        # Line comment — drop it, leaving a space so tokens don't merge.
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            buf.append(" ")
            continue

        # Block comment — drop it, leaving a space.
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            buf.append(" ")
            continue

        # Single-quoted string literal (with '' escape)
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Double-quoted identifier
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == '"':
                    i += 1
                    break
                i += 1
            continue

        # Track BEGIN/END compound blocks (case-insensitive, word boundary)
        if ch.isalpha():
            word = _word_at(i)
            if word is not None:
                upper = word.upper()
                if upper == "BEGIN":
                    block_depth += 1
                elif upper == "END" and block_depth > 0:
                    block_depth -= 1
                buf.append(sql[i : i + len(word)])
                i += len(word)
                continue

        # Statement terminator (only at top level, not inside a trigger body)
        if ch == ";" and block_depth == 0:
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# --------------------------------------------------------------------------------------
# Migration discovery & validation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


class MigrationError(RuntimeError):
    """Raised for any migration ordering/integrity problem."""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migrations sorted by version, validating naming and contiguity.

    Filenames must match ``NNN_snake_case.sql``. Versions must be contiguous starting
    at 1 with no gaps or duplicates — this is what makes an "out-of-order" or missing
    file an immediate, detectable error (CONVENTIONS §13).
    """
    if not migrations_dir.exists():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    found: dict[int, Migration] = {}
    for path in sorted(migrations_dir.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix != ".sql":
            raise MigrationError(f"unexpected non-.sql file in migrations dir: {path.name}")
        m = _MIGRATION_NAME_RE.match(path.name)
        if m is None:
            raise MigrationError(f"migration filename must match NNN_snake_case.sql: {path.name}")
        version = int(m.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version:03d}")
        text = path.read_text(encoding="utf-8")
        found[version] = Migration(
            version=version, name=path.name, path=path, sql=text, checksum=_checksum(text)
        )

    if not found:
        return []

    versions = sorted(found)
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 001; got {versions}, expected {expected}"
        )
    return [found[v] for v in versions]


# --------------------------------------------------------------------------------------
# Migration runner
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: list[int]
    already_current: bool
    current_version: int
    snapshots: list[Path]


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _applied(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def current_version(db_path: Path) -> int:
    """Highest applied migration version, or 0 if none/fresh."""
    conn = connect(db_path)
    try:
        _ensure_bookkeeping(conn)
        applied = _applied(conn)
        return max(applied) if applied else 0
    finally:
        conn.close()


def _snapshot(db_path: Path, backup_dir: Path, version: int) -> Path:
    """Take a VACUUM INTO snapshot before applying ``version``.

    VACUUM INTO cannot run inside a transaction, so it uses its own autocommit
    connection. The snapshot filename encodes the target version.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"pre_migration_v{version:03d}.db"
    # Overwrite any stale snapshot from a previous failed attempt at the same version.
    if target.exists():
        target.unlink()
    snap_conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        snap_conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        snap_conn.close()
    return target


def run_migrations(
    db_path: Path,
    migrations_dir: Path = MIGRATIONS_DIR,
    *,
    backup_dir: Path | None = None,
) -> MigrationResult:
    """Apply all pending migrations in order. Idempotent; safe to call at startup.

    Guarantees:
      * refuses out-of-order/gapped/duplicate migration files (via discovery);
      * refuses to run if the database is *ahead* of the code or a previously applied
        migration's checksum no longer matches (tamper/rollback detection);
      * snapshots the database with VACUUM INTO before every apply;
      * applies each migration atomically in a single transaction; a failing migration
        leaves the database at the previous version.
    """
    migrations = discover_migrations(migrations_dir)
    if backup_dir is None:
        backup_dir = db_path.parent / "backups" / "pre_migration"

    conn = connect(db_path)
    try:
        _ensure_bookkeeping(conn)
        applied = _applied(conn)

        # Integrity: applied versions must be a prefix {1..k} and match file checksums.
        for version, checksum in applied.items():
            match = next((m for m in migrations if m.version == version), None)
            if match is None:
                raise MigrationError(
                    f"database has applied migration {version:03d} with no matching file "
                    "(database is ahead of the code — refusing to run)"
                )
            if match.checksum != checksum:
                raise MigrationError(
                    f"migration {version:03d} checksum mismatch: applied file was modified "
                    "after being applied (refusing to run)"
                )
        if applied:
            expected_prefix = set(range(1, max(applied) + 1))
            if set(applied) != expected_prefix:
                raise MigrationError(
                    f"applied migrations are not a contiguous prefix: {sorted(applied)}"
                )

        pending = [m for m in migrations if m.version not in applied]
        if not pending:
            return MigrationResult(
                applied=[],
                already_current=True,
                current_version=max(applied) if applied else 0,
                snapshots=[],
            )

        snapshots: list[Path] = []
        applied_now: list[int] = []
        for migration in pending:
            snapshots.append(_snapshot(db_path, backup_dir, migration.version))
            statements = split_sql_statements(migration.sql)
            try:
                conn.execute("BEGIN")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.checksum),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                log.error("migration_failed", version=migration.version, name=migration.name)
                raise
            applied_now.append(migration.version)
            log.info("migration_applied", version=migration.version, name=migration.name)

        return MigrationResult(
            applied=applied_now,
            already_current=False,
            current_version=applied_now[-1],
            snapshots=snapshots,
        )
    finally:
        conn.close()
