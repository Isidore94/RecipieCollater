"""Ingestion jobs: URL normalization, idempotent enqueue, lifecycle, and artifacts.

A submitted URL is normalized to a stable key so a resubmission or a worker replay finds the
existing job instead of creating a duplicate (docs/04 section 2). Artifacts are immutable,
content-addressed (sha256), gzip-compressed captures under data/artifacts. The extraction
pipeline itself runs in the worker (later slices); this module owns the request side and the
job record.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import get_settings
from app.security import now_iso

ACTIVE_STATUSES: tuple[str, ...] = ("queued", "fetching", "extracting", "normalizing")

_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "mc_cid", "mc_eid", "igshid", "si", "feature",
    }
)
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
# The four path prefixes that address a single post. A bare /<username> is a profile, not a post,
# and must not match.
_INSTAGRAM_PREFIXES = ("/reel/", "/reels/", "/p/", "/tv/")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")


class IngestError(ValueError):
    """A submitted URL could not be accepted (empty, bad scheme, or unparseable)."""


@dataclass(frozen=True, slots=True)
class IngestJob:
    id: int
    url: str
    normalized_url: str
    source: str
    has_html: bool
    status: str
    attempts: int
    error_category: str | None
    error_message: str | None
    recipe_id: int | None
    submitted_by: int | None
    created_at: str
    updated_at: str


# --------------------------------------------------------------------------------------
# URL normalization
# --------------------------------------------------------------------------------------


def youtube_video_id(url: str) -> str | None:
    """Return the 11-ish char YouTube id for a watch/short/embed/youtu.be URL, else None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        return parts.path.lstrip("/").split("/")[0] or None
    if host in _YOUTUBE_HOSTS:
        if parts.path == "/watch":
            return dict(parse_qsl(parts.query)).get("v")
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if parts.path.startswith(prefix):
                return parts.path[len(prefix) :].split("/")[0] or None
    return None


def instagram_shortcode(url: str) -> str | None:
    """Return the shortcode for a reel/post/IGTV URL, else None.

    ``/p/<code>`` and ``/reel/<code>`` address the *same* post: Instagram's own app shares one
    form and the website the other. Both must collapse to one idempotency key, or sharing the
    same reel twice by two routes would create two recipes.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _INSTAGRAM_HOSTS:
        return None
    path = parts.path
    for prefix in _INSTAGRAM_PREFIXES:
        if path.startswith(prefix):
            code = path[len(prefix) :].split("/")[0]
            return code if _SHORTCODE_RE.match(code) else None
    return None


def normalize_url(raw: str) -> str:
    """Normalize a URL to a stable idempotency key (canonical YouTube, no trackers/fragment)."""
    text = raw.strip()
    if not text:
        raise IngestError("empty URL")
    if "://" not in text:
        text = "https://" + text  # tolerate a bare host/path paste
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port  # raises ValueError on a non-numeric "port" (e.g. javascript:alert(1))
    except ValueError as exc:
        raise IngestError(f"could not parse URL: {raw!r}") from exc
    if parts.scheme not in ("http", "https"):
        raise IngestError(f"unsupported URL scheme: {parts.scheme!r}")
    if not host:
        raise IngestError("URL has no host")

    video_id = youtube_video_id(text)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"

    shortcode = instagram_shortcode(text)
    if shortcode:
        return f"https://www.instagram.com/reel/{shortcode}"

    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, netloc, path, urlencode(kept), ""))


# --------------------------------------------------------------------------------------
# Artifacts (immutable, content-addressed)
# --------------------------------------------------------------------------------------


def store_artifact(conn: sqlite3.Connection, job_id: int, kind: str, data: bytes) -> str:
    """Persist an immutable, gzip-compressed, content-addressed artifact; return its sha256."""
    sha = hashlib.sha256(data).hexdigest()
    root = get_settings().artifacts_dir
    folder = root / sha[:2]
    folder.mkdir(parents=True, exist_ok=True)
    blob = folder / sha
    if not blob.exists():
        blob.write_bytes(gzip.compress(data))
    conn.execute(
        "INSERT OR REPLACE INTO artifacts (job_id, kind, sha256, path, bytes) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, kind, sha, f"{sha[:2]}/{sha}", len(data)),
    )
    conn.commit()
    return sha


def read_artifact(conn: sqlite3.Connection, job_id: int, kind: str) -> bytes | None:
    row = conn.execute(
        "SELECT path FROM artifacts WHERE job_id = ? AND kind = ?", (job_id, kind)
    ).fetchone()
    if row is None:
        return None
    blob = get_settings().artifacts_dir / row["path"]
    return gzip.decompress(blob.read_bytes()) if blob.is_file() else None


# --------------------------------------------------------------------------------------
# Enqueue & lifecycle
# --------------------------------------------------------------------------------------


def _row_to_job(row: sqlite3.Row) -> IngestJob:
    return IngestJob(
        id=int(row["id"]), url=row["url"], normalized_url=row["normalized_url"],
        source=row["source"], has_html=bool(row["has_html"]), status=row["status"],
        attempts=int(row["attempts"]), error_category=row["error_category"],
        error_message=row["error_message"], recipe_id=row["recipe_id"],
        submitted_by=row["submitted_by"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def enqueue_job(
    conn: sqlite3.Connection,
    url: str,
    *,
    html: str | None = None,
    submitted_by: int | None = None,
    source: str = "api",
) -> tuple[IngestJob, bool]:
    """Create an ingest job (or return the existing one for a duplicate URL)."""
    normalized = normalize_url(url)
    try:
        cur = conn.execute(
            """INSERT INTO ingest_jobs
               (url, normalized_url, idempotency_key, source, has_html, submitted_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (url.strip(), normalized, normalized, source, 1 if html else 0, submitted_by),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM ingest_jobs WHERE idempotency_key = ?", (normalized,)
        ).fetchone()
        if existing is None:
            raise
        if existing["status"] == "failed":
            # Re-pasting a failed URL is the user saying "try again" (a fetch may have been
            # transient, or extraction has improved since). Requeue it - returning the dead
            # job left the URL permanently stuck.
            conn.execute(
                "UPDATE ingest_jobs SET status = 'queued', error_category = NULL, "
                "error_message = NULL, updated_at = datetime('now') WHERE id = ?",
                (existing["id"],),
            )
            if html:
                store_artifact(conn, int(existing["id"]), "supplied_html", html.encode("utf-8"))
                conn.execute(
                    "UPDATE ingest_jobs SET has_html = 1 WHERE id = ?", (existing["id"],)
                )
            conn.commit()
            requeued = get_job(conn, int(existing["id"]))
            assert requeued is not None
            return requeued, True  # created=True so the caller schedules processing
        return _row_to_job(existing), False
    job_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    if html:
        store_artifact(conn, job_id, "supplied_html", html.encode("utf-8"))
    job = get_job(conn, job_id)
    assert job is not None
    return job, True


def set_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    error_category: str | None = None,
    error_message: str | None = None,
    recipe_id: int | None = None,
) -> None:
    stamp = now_iso()
    conn.execute(
        """UPDATE ingest_jobs
           SET status = ?, error_category = ?, error_message = ?,
               recipe_id = COALESCE(?, recipe_id), last_heartbeat_at = ?, updated_at = ?
           WHERE id = ?""",
        (status, error_category, error_message, recipe_id, stamp, stamp, job_id),
    )
    conn.commit()


def heartbeat(conn: sqlite3.Connection, job_id: int) -> None:
    stamp = now_iso()
    conn.execute(
        "UPDATE ingest_jobs SET last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
        (stamp, stamp, job_id),
    )
    conn.commit()


def increment_attempts(conn: sqlite3.Connection, job_id: int) -> int:
    conn.execute(
        "UPDATE ingest_jobs SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (now_iso(), job_id),
    )
    conn.commit()
    row = conn.execute("SELECT attempts FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()
    return int(row["attempts"]) if row else 0


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------


def get_job(conn: sqlite3.Connection, job_id: int) -> IngestJob | None:
    row = conn.execute("SELECT * FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_active_jobs(conn: sqlite3.Connection) -> list[IngestJob]:
    rows = conn.execute(
        "SELECT * FROM ingest_jobs "
        "WHERE status IN ('queued', 'fetching', 'extracting', 'normalizing') "
        "ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def list_pending_jobs(conn: sqlite3.Connection, *, limit: int = 25) -> list[IngestJob]:
    """Jobs the inbox should surface: everything not yet 'done' (in-flight or failed).

    A successful job becomes an inbox recipe card, so it drops out of this list on completion.
    """
    rows = conn.execute(
        "SELECT * FROM ingest_jobs WHERE status != 'done' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_job(r) for r in rows]
