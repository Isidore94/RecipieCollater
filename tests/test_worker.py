"""Worker plumbing: Huey uses a SEPARATE queue.db and tasks execute."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from huey import SqliteHuey


@pytest.fixture
def worker_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from app import config

    monkeypatch.setenv("RC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RC_HUEY_IMMEDIATE", "1")  # execute synchronously for the test
    monkeypatch.setenv("RC_LOG_CONSOLE", "1")
    config.reset_settings_cache()
    import app.tasks as tasks_module

    reloaded = importlib.reload(tasks_module)
    yield reloaded
    config.reset_settings_cache()


def test_queue_path_is_separate_from_app_db(worker_module, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from app import config

    settings = config.get_settings()
    assert settings.queue_db_path == worker_module.QUEUE_DB_PATH
    assert worker_module.QUEUE_DB_PATH.name == "queue.db"
    assert settings.db_path != worker_module.QUEUE_DB_PATH


def test_ping_task_executes(worker_module) -> None:  # type: ignore[no-untyped-def]
    assert worker_module.ping("hello").get() == "hello"


def test_sqlitehuey_persists_to_its_own_file(tmp_path: Path) -> None:
    """A non-immediate SqliteHuey writes the enqueued task to the given file, proving
    the queue is a real separate database rather than sharing the app DB."""
    queue_path = tmp_path / "queue.db"
    h = SqliteHuey(filename=str(queue_path))

    @h.task()
    def noop() -> int:
        return 1

    noop()  # enqueue only (no consumer running)
    assert queue_path.exists()
