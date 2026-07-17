"""Token generation/hashing and time helpers.

Tokens are opaque random strings; only their SHA-256 hash is ever stored (CONVENTIONS §6).
Timestamps are stored as UTC strings in SQLite's ``datetime('now')`` format so that
string comparison is chronological.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

# One persistent cookie (CONVENTIONS §5).
SESSION_COOKIE_NAME = "rc_session"
SESSION_LIFETIME = timedelta(days=400)
SESSION_RENEW_AFTER = timedelta(days=30)
ONBOARDING_LIFETIME = timedelta(minutes=15)

# Custom header that a cross-site HTML form cannot set (CONVENTIONS §7).
CSRF_HEADER = "x-rc-csrf"

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().strftime(_TS_FMT)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TS_FMT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _TS_FMT).replace(tzinfo=UTC)


def generate_token() -> str:
    """A fresh opaque token (URL-safe, ~256 bits)."""
    return secrets.token_urlsafe(32)


def generate_pairing_code() -> str:
    """A short human-typable code (unambiguous alphabet, 6 chars)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
    return "".join(secrets.choice(alphabet) for _ in range(6))


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
