"""Unit tests for the PIN credential service (CONVENTIONS §6, §15)."""

from __future__ import annotations

import sqlite3

import pytest

from app.security import now, to_iso
from app.services import credentials
from app.services.users import User, create_user


def _mk_user(conn: sqlite3.Connection, name: str = "Aaron", *, admin: bool = True) -> User:
    return create_user(conn, name, is_admin=admin)


def test_hash_and_verify_roundtrip() -> None:
    encoded = credentials.hash_pin("1234")
    assert encoded.startswith("scrypt$")
    assert credentials.verify_pin("1234", encoded)
    assert not credentials.verify_pin("4321", encoded)


def test_hash_is_salted() -> None:
    a = credentials.hash_pin("1234")
    b = credentials.hash_pin("1234")
    assert a != b  # random per-hash salt
    assert credentials.verify_pin("1234", a)
    assert credentials.verify_pin("1234", b)


@pytest.mark.parametrize("bad", ["", "12", "abc", "12a4", "1" * 13, "12 3"])
def test_validate_pin_rejects_bad(bad: str) -> None:
    with pytest.raises(credentials.PinError):
        credentials.validate_pin(bad)


@pytest.mark.parametrize("good", ["1234", "000000", "12345678"])
def test_validate_pin_accepts_good(good: str) -> None:
    assert credentials.validate_pin(good) == good


def test_verify_pin_rejects_malformed() -> None:
    assert not credentials.verify_pin("1234", "not-a-hash")
    assert not credentials.verify_pin("1234", "scrypt$bad")
    assert not credentials.verify_pin("1234", "scrypt$x$8$1$aa$bb")


def test_set_and_check_login(migrated_db: sqlite3.Connection) -> None:
    user = _mk_user(migrated_db)
    credentials.set_pin(migrated_db, user.id, "2468")
    assert credentials.has_pin(migrated_db, user.id)

    ok = credentials.check_login(migrated_db, "Aaron", "2468")
    assert ok.ok and ok.user_id == user.id

    bad = credentials.check_login(migrated_db, "Aaron", "9999")
    assert not bad.ok and bad.user_id is None


def test_check_login_unknown_user_or_no_pin(migrated_db: sqlite3.Connection) -> None:
    _mk_user(migrated_db, "NoPin", admin=False)
    assert not credentials.check_login(migrated_db, "Ghost", "1234").ok
    assert not credentials.check_login(migrated_db, "NoPin", "1234").ok


def test_lockout_after_max_attempts(migrated_db: sqlite3.Connection) -> None:
    user = _mk_user(migrated_db)
    credentials.set_pin(migrated_db, user.id, "1357")
    result = credentials.LoginResult(ok=False)
    for _ in range(credentials.MAX_FAILED_ATTEMPTS):
        result = credentials.check_login(migrated_db, "Aaron", "0000")
    assert result.locked  # the threshold-th wrong attempt trips the lock
    # even the correct PIN is refused while locked
    blocked = credentials.check_login(migrated_db, "Aaron", "1357")
    assert blocked.locked and not blocked.ok


def test_lock_expiry_allows_retry(migrated_db: sqlite3.Connection) -> None:
    user = _mk_user(migrated_db)
    credentials.set_pin(migrated_db, user.id, "1357")
    # Force a lock that has already expired.
    past = to_iso(now() - credentials.LOCKOUT_DURATION)
    migrated_db.execute(
        "UPDATE users SET pin_locked_until = ?, pin_failed_attempts = 0 WHERE id = ?",
        (past, user.id),
    )
    migrated_db.commit()
    result = credentials.check_login(migrated_db, "Aaron", "1357")
    assert result.ok
    row = migrated_db.execute(
        "SELECT pin_failed_attempts, pin_locked_until FROM users WHERE id = ?", (user.id,)
    ).fetchone()
    assert row["pin_failed_attempts"] == 0 and row["pin_locked_until"] is None


def test_successful_login_resets_failed_attempts(migrated_db: sqlite3.Connection) -> None:
    user = _mk_user(migrated_db)
    credentials.set_pin(migrated_db, user.id, "1357")
    credentials.check_login(migrated_db, "Aaron", "0000")  # one failure
    credentials.check_login(migrated_db, "Aaron", "1357")  # success resets the counter
    row = migrated_db.execute(
        "SELECT pin_failed_attempts FROM users WHERE id = ?", (user.id,)
    ).fetchone()
    assert row["pin_failed_attempts"] == 0


def test_clear_pin(migrated_db: sqlite3.Connection) -> None:
    user = _mk_user(migrated_db)
    credentials.set_pin(migrated_db, user.id, "1357")
    credentials.clear_pin(migrated_db, user.id)
    assert not credentials.has_pin(migrated_db, user.id)
    assert not credentials.check_login(migrated_db, "Aaron", "1357").ok


def test_set_pin_validates(migrated_db: sqlite3.Connection) -> None:
    user = _mk_user(migrated_db)
    with pytest.raises(credentials.PinError):
        credentials.set_pin(migrated_db, user.id, "12")


def test_ids_and_names_with_pin(migrated_db: sqlite3.Connection) -> None:
    a = _mk_user(migrated_db, "Aaron")
    b = _mk_user(migrated_db, "Bea", admin=False)
    _mk_user(migrated_db, "NoPin", admin=False)
    credentials.set_pin(migrated_db, a.id, "1111")
    credentials.set_pin(migrated_db, b.id, "2222")
    assert credentials.user_ids_with_pin(migrated_db) == {a.id, b.id}
    assert credentials.names_with_pin(migrated_db) == ["Aaron", "Bea"]
