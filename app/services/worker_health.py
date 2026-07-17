"""Small on-disk heartbeat shared by the Huey worker and web health endpoint."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta

from app.config import Settings
from app.security import now, now_iso, parse_iso

MAX_HEARTBEAT_AGE = timedelta(minutes=3)


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    recorded_at: str
    release_id: str
    age_seconds: float


def write_heartbeat(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    target = settings.worker_heartbeat_path
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"recorded_at": now_iso(), "release_id": settings.release_id}),
        encoding="utf-8",
    )
    temporary.replace(target)


def read_heartbeat(settings: Settings) -> WorkerHeartbeat | None:
    try:
        payload = json.loads(settings.worker_heartbeat_path.read_text(encoding="utf-8"))
        recorded_at = str(payload["recorded_at"])
        release = str(payload["release_id"])
        age = (now() - parse_iso(recorded_at)).total_seconds()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return WorkerHeartbeat(recorded_at=recorded_at, release_id=release, age_seconds=age)
