"""RecipeCollater desktop launcher — one runnable that hosts the whole app on the mini PC.

Modes (selected by argv):
  --web         run only the web server (uvicorn) in this process
  --worker      run only the Huey worker (ingestion) in this process
  --smoke-test  start the web server on an ephemeral port, hit /healthz, exit 0/1 (build check)
  (no args)     a small always-on control window that supervises a --web and a --worker child

The web and worker stay as *separate processes* (Huey needs its own process for signal handling and
its own queue.db, per CONVENTIONS 3); the GUI just spawns and watches them. Packaged with
PyInstaller (deploy/windows/RecipeCollater.spec) this becomes recipecollater.exe. Configuration -
the RC_* variables, including the AI keys - is read from a .env placed beside the executable, never
embedded in it.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

# --------------------------------------------------------------------------------------
# Environment / paths
# --------------------------------------------------------------------------------------


def executable_dir() -> Path:
    """The folder the app is launched from: beside the .exe when frozen, else the repo root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_env_file() -> None:
    """Load KEY=VALUE lines from a .env beside the executable. Existing env vars always win."""
    env_path = executable_dir() / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def lan_ip() -> str:
    """Best-effort LAN IPv4 of this machine (the address other devices reach it at)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # no packets sent; just picks the outbound interface
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def web_host() -> str:
    return os.environ.get("RC_HOST", "0.0.0.0")  # noqa: S104 - a LAN server binds all interfaces


def web_port() -> int:
    try:
        return int(os.environ.get("RC_PORT", "80"))
    except ValueError:
        return 80


def base_url() -> str:
    return os.environ.get("APP_BASE_URL", f"http://{lan_ip()}:{web_port()}").rstrip("/")


def _child_command(mode: str) -> list[str]:
    """argv to relaunch this program in a single-purpose child mode (frozen exe or dev module)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{mode}"]
    return [sys.executable, "-m", "app.desktop", f"--{mode}"]


# --------------------------------------------------------------------------------------
# Single-purpose run modes
# --------------------------------------------------------------------------------------


def run_web() -> None:
    """Run the web server (blocks). Its lifespan applies pending migrations on startup."""
    import uvicorn

    from app.main import app

    uvicorn.run(app, host=web_host(), port=web_port(), log_level="info")


def run_worker() -> None:
    """Run the Huey ingestion worker (blocks). Must be its own process (signals + queue.db)."""
    from huey.consumer import Consumer

    from app.tasks import huey

    Consumer(huey, workers=1, worker_type="thread").run()


def run_smoke_test() -> int:
    """Prove the bundled web stack serves: fresh-migrate a throwaway DB, start uvicorn on an
    ephemeral port, and confirm /healthz answers. Isolated in a temp data dir so it never touches
    real data; a 503 (no worker yet) still counts - the point is to catch bundling failures."""
    import tempfile
    import time
    import urllib.error
    import urllib.request

    os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc-smoke-")

    import uvicorn

    from app.main import app  # imported after RC_DATA_DIR is set

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            print("SMOKE FAIL: server never started")
            return 1
        time.sleep(0.1)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=10) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code  # the server answered with an error status; the web stack still works
    except Exception as exc:  # a connection error means the bundle is broken
        print(f"SMOKE FAIL: /healthz error: {exc}")
        return 1
    finally:
        server.should_exit = True

    ok = code in (200, 503)
    print(f"SMOKE OK (/healthz {code})" if ok else f"SMOKE FAIL: /healthz {code}")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------
# GUI supervisor (default mode)
# --------------------------------------------------------------------------------------


def run_gui() -> None:
    from app.desktop_gui import main as gui_main

    gui_main(_child_command)


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = sys.argv[1:] if argv is None else argv
    if "--web" in args:
        run_web()
        return 0
    if "--worker" in args:
        run_worker()
        return 0
    if "--smoke-test" in args:
        return run_smoke_test()
    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
