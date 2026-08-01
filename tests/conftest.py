"""Shared pytest fixtures. All tests run fully offline (CONVENTIONS §15)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the app at an isolated temp data directory and reset the settings cache."""
    d = tmp_path / "data"
    monkeypatch.setenv("RC_DATA_DIR", str(d))
    monkeypatch.setenv("RC_LOG_CONSOLE", "1")
    monkeypatch.setenv("RC_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("RC_SETUP_TOKEN", "test-setup-token")
    monkeypatch.delenv("RC_HTTPS", raising=False)
    monkeypatch.delenv("RC_BACKUP_DIR", raising=False)
    # A developer shell often exports the real provider keys (the prod launcher pulls them from
    # the User registry). Tests must decide for themselves whether AI is enabled, or the
    # "no key configured" paths silently stop being exercised on that machine.
    monkeypatch.delenv("RC_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RC_OPENAI_API_KEY", raising=False)
    config.reset_settings_cache()
    yield d
    config.reset_settings_cache()


@pytest.fixture
def migrated_db(data_dir: Path) -> Iterator[sqlite3.Connection]:
    """A migrated app database with an open connection."""
    db_path = data_dir / "recipecollater.db"
    db.run_migrations(db_path, backup_dir=data_dir / "backups" / "pre_migration")
    conn = db.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    """A TestClient whose lifespan migrates the temp database on startup."""
    from app.main import create_app
    from app.services.worker_health import write_heartbeat

    write_heartbeat(config.get_settings())
    with TestClient(create_app()) as c:
        yield c


# Fetch-Metadata header that mimics a same-origin browser request (passes CSRF).
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    """A client that has completed first-run setup and holds an admin session cookie."""
    resp = client.post(
        "/setup",
        data={
            "admin_name": "Aaron",
            "device_name": "Kitchen PC",
            "setup_token": "test-setup-token",
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "rc_session" in resp.headers.get("set-cookie", "")
    return client
