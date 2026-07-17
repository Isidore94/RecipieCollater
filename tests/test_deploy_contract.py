"""Static invariants for the root-run deployment scripts.

Full systemd behavior is hand-tested on the N95, but these checks prevent a later agent from
silently reintroducing the unsafe hot-copy or in-place dependency update patterns.
"""

from __future__ import annotations

from app.config import REPO_ROOT


def _script(name: str) -> str:
    return (REPO_ROOT / "deploy" / name).read_text(encoding="utf-8")


def test_update_uses_consistent_snapshot_and_stopped_cutover() -> None:
    script = _script("update.sh")
    assert 'cp -a "$DATA/."' not in script
    assert "snapshot-db" in script
    stop_at = script.index("systemctl stop")
    live_migrate_at = script.index('echo "==> [7/7] Migrating live data', stop_at)
    assert stop_at < live_migrate_at
    assert "mv -Tf" in script
    assert '. "$ETC/env"' in script
    assert "RC_BACKUP_DIR" in script


def test_release_environments_are_locked_and_not_owned_by_service_user() -> None:
    install = _script("install.sh")
    update = _script("update.sh")
    assert "uv sync --no-dev --frozen" in install
    assert "uv sync --no-dev --frozen" in update
    assert 'chown -R "$APP_USER:$APP_USER" "$RELEASE"' not in install
    assert 'chown -R "$APP_USER:$APP_USER" "$RELEASE"' not in update
