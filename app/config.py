"""Runtime configuration, read once from the environment.

Kept dependency-free (no pydantic-settings) on purpose — the surface is tiny and
`CONVENTIONS.md` favours fewer dependencies. Secrets are read from the environment
only; they never appear in the repo, database, or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Repository root (…/recipecollater). Used to locate migrations, templates, static, seed.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable process configuration."""

    # Single source of truth for links, Shortcut templates, callbacks (CONVENTIONS §11).
    app_base_url: str
    # Directory holding the SQLite databases, images, artifacts, and backups.
    data_dir: Path
    # Whether the app is served over HTTPS (mkcert/Tailscale upgrade). Controls the
    # `Secure` cookie flag. Default False = plain HTTP on the LAN.
    https_enabled: bool
    # Emit human-readable console logs instead of JSON (handy in a terminal / tests).
    log_console: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "recipecollater.db"

    @property
    def queue_db_path(self) -> Path:
        # The Huey queue is a *separate* SQLite file (CONVENTIONS §3).
        return self.data_dir / "queue.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def cookie_secure(self) -> bool:
        return self.https_enabled

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.images_dir,
            self.artifacts_dir,
            self.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("RC_DATA_DIR", str(REPO_ROOT / "data"))).resolve()
    return Settings(
        app_base_url=os.environ.get("APP_BASE_URL", "http://recipes.local").rstrip("/"),
        data_dir=data_dir,
        https_enabled=_env_bool("RC_HTTPS", False),
        log_console=_env_bool("RC_LOG_CONSOLE", False),
    )


def reset_settings_cache() -> None:
    """Clear the cached settings (tests set env vars then rebuild)."""
    get_settings.cache_clear()
