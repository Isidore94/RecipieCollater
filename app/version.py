"""Application and deployed-release identity.

``PACKAGE_VERSION`` describes the Python package/API compatibility. ``release_id`` identifies
the exact staged checkout (normally its commit SHA), so health checks and backup manifests can
prove which release is actually running.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_VERSION = "0.0.0"


def release_id(repo_root: Path) -> str:
    configured = os.environ.get("RC_RELEASE_ID", "").strip()
    if configured:
        return configured
    marker = repo_root / ".release-id"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "development"
