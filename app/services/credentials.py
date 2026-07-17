"""Optional numeric PIN sign-in for named users.

Passwordless device pairing remains the default (CONVENTIONS §5). This adds an OPTIONAL
per-user PIN so a household member can sign in by name + PIN on a new device. The PIN is
never stored in the clear — only a salted, self-describing scrypt hash (CONVENTIONS §6 now
covers PINs). Because a numeric PIN is low-entropy, brute force is bounded by a per-user
lockout. A correct PIN only *mints a device session*; the rc_session cookie stays the
single identity authority.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from app.security import now, now_iso, parse_iso, to_iso

MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 12
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

# scrypt cost (RFC 7914). N must be a power of two; memory ≈ 128 * N * r bytes (~16 MB here).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16


class PinError(ValueError):
    """A PIN failed validation (wrong length or not all digits)."""


@dataclass(frozen=True, slots=True)
class LoginResult:
    ok: bool
    user_id: int | None = None
    locked: bool = False
    locked_until: str | None = None


def validate_pin(pin: str) -> str:
    """Return the cleaned PIN or raise PinError. PINs are digits only, bounded length."""
    pin = pin.strip()
    if not pin.isdigit():
        raise PinError("PIN must be digits only.")
    if not MIN_PIN_LENGTH <= len(pin) <= MAX_PIN_LENGTH:
        raise PinError(f"PIN must be {MIN_PIN_LENGTH} to {MAX_PIN_LENGTH} digits.")
    return pin


def _derive(pin: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM
    )


def hash_pin(pin: str) -> str:
    """Validate then hash a PIN into a self-describing ``scrypt$n$r$p$salt$hash`` string."""
    clean = validate_pin(pin)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(clean, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, encoded: str) -> bool:
    """Constant-time check of ``pin`` against a stored ``scrypt$...`` string."""
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False
    candidate = _derive(pin.strip(), salt, n, r, p)
    return hmac.compare_digest(candidate, expected)


@lru_cache(maxsize=1)
def _dummy_encoded() -> str:
    return hash_pin("000000")


def _equalize_timing() -> None:
    # Run one derivation on the no-user / no-PIN path so response time does not leak whether
    # a given name exists or has a PIN.
    verify_pin("000000", _dummy_encoded())


def has_pin(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT pin_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row is not None and row["pin_hash"])


def user_ids_with_pin(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT id FROM users WHERE pin_hash IS NOT NULL").fetchall()
    return {int(row["id"]) for row in rows}


def names_with_pin(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM users WHERE pin_hash IS NOT NULL ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [str(row["name"]) for row in rows]


def set_pin(conn: sqlite3.Connection, user_id: int, pin: str) -> None:
    """Set (or replace) a user's PIN and clear any failed-attempt / lockout state."""
    encoded = hash_pin(pin)  # validates; raises PinError on bad input
    conn.execute(
        """UPDATE users
           SET pin_hash = ?, pin_set_at = ?, pin_failed_attempts = 0, pin_locked_until = NULL
           WHERE id = ?""",
        (encoded, now_iso(), user_id),
    )
    conn.commit()


def clear_pin(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        """UPDATE users
           SET pin_hash = NULL, pin_set_at = NULL, pin_failed_attempts = 0, pin_locked_until = NULL
           WHERE id = ?""",
        (user_id,),
    )
    conn.commit()


def check_login(conn: sqlite3.Connection, name: str, pin: str) -> LoginResult:
    """Verify a name + PIN with per-user lockout. Never reveals which half was wrong."""
    row = conn.execute(
        "SELECT id, pin_hash, pin_failed_attempts, pin_locked_until FROM users WHERE name = ?",
        (name.strip(),),
    ).fetchone()
    if row is None or not row["pin_hash"]:
        _equalize_timing()
        return LoginResult(ok=False)

    current = now()
    locked_until = row["pin_locked_until"]
    if locked_until is not None and parse_iso(locked_until) > current:
        return LoginResult(ok=False, locked=True, locked_until=locked_until)

    user_id = int(row["id"])
    if verify_pin(pin, row["pin_hash"]):
        conn.execute(
            "UPDATE users SET pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?",
            (user_id,),
        )
        conn.commit()
        return LoginResult(ok=True, user_id=user_id)

    attempts = int(row["pin_failed_attempts"]) + 1
    if attempts >= MAX_FAILED_ATTEMPTS:
        new_lock = to_iso(current + LOCKOUT_DURATION)
        conn.execute(
            "UPDATE users SET pin_failed_attempts = 0, pin_locked_until = ? WHERE id = ?",
            (new_lock, user_id),
        )
        conn.commit()
        return LoginResult(ok=False, locked=True, locked_until=new_lock)

    conn.execute(
        "UPDATE users SET pin_failed_attempts = ? WHERE id = ?",
        (attempts, user_id),
    )
    conn.commit()
    return LoginResult(ok=False)
