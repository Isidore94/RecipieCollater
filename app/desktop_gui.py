"""Tkinter control window for the RecipeCollater desktop app.

A small always-on supervisor: it starts the web server and the Huey worker as child processes,
shows their status and the LAN address other devices use, streams their logs into an activity box,
and stops them cleanly on close. It owns no business logic - everything runs in the children, which
are the same code paths as the headless --web/--worker modes.
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from tkinter import ttk

from app.desktop import base_url, executable_dir, web_port

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SERVICES = ("web", "worker")


def _acquire_single_instance() -> bool:
    """Named Windows mutex so two supervisors can't fight over the port and queue."""
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Local\\RecipeCollaterDesktopApp")
    return bool(kernel32.GetLastError() != 183)  # ERROR_ALREADY_EXISTS


class SupervisorApp:
    def __init__(self, root: tk.Tk, child_command: Callable[[str], list[str]]) -> None:
        self.root = root
        self.child_command = child_command
        self.procs: dict[str, subprocess.Popen[str]] = {}
        self.logs: queue.Queue[str] = queue.Queue()
        self.url = base_url()
        self.closing = False

        self.status_var = tk.StringVar(value="Starting…")
        self.url_var = tk.StringVar(value=self.url)

        root.title("RecipeCollater")
        root.geometry("760x520")
        root.minsize(620, 420)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()

        self.root.after(200, self._drain_logs)
        self.root.after(400, self._poll_status)
        self.root.after(300, self.start_all)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        ttk.Label(outer, text="RecipeCollater", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(outer, textvariable=self.status_var).grid(row=0, column=0, sticky="e")

        url_row = ttk.Frame(outer)
        url_row.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Label(url_row, text="Open on this network:").pack(side="left")
        ttk.Label(url_row, textvariable=self.url_var, font=("Segoe UI", 10, "bold")).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(url_row, text="Open", width=7, command=self.open_url).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(url_row, text="Copy link", width=10, command=self.copy_url).pack(
            side="left", padx=(4, 0)
        )

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.toggle_button = ttk.Button(controls, text="Stop", command=self.toggle)
        self.toggle_button.pack(side="left")
        ttk.Label(
            controls,
            text=f"Web + worker run in the background. Port {web_port()}.",
        ).pack(side="left", padx=(12, 0))

        log_box = ttk.LabelFrame(outer, text="Activity", padding=8)
        log_box.grid(row=3, column=0, sticky="nsew")
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_box, height=14, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    # -- process supervision ----------------------------------------------------------

    def start_all(self) -> None:
        os.environ.setdefault("APP_BASE_URL", self.url)
        for name in _SERVICES:
            self._start(name)

    def _start(self, name: str) -> None:
        existing = self.procs.get(name)
        if existing is not None and existing.poll() is None:
            return
        try:
            proc = subprocess.Popen(  # noqa: S603 - command is built by us, never user input
                self.child_command(name),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(executable_dir()),
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.logs.put_nowait(f"[{name}] failed to start: {exc}")
            return
        self.procs[name] = proc
        threading.Thread(target=self._pump, args=(name, proc), daemon=True).start()
        self.logs.put_nowait(f"[{name}] started (pid {proc.pid})")

    def _pump(self, name: str, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.logs.put_nowait(f"[{name}] {line.rstrip()}")
        self.logs.put_nowait(f"[{name}] exited (code {proc.wait()})")

    def stop_all(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        self.procs.clear()

    def toggle(self) -> None:
        if any(p.poll() is None for p in self.procs.values()):
            self.stop_all()
            self.toggle_button.configure(text="Start")
        else:
            self.start_all()
            self.toggle_button.configure(text="Stop")

    # -- periodic UI ticks ------------------------------------------------------------

    def _poll_status(self) -> None:
        if not self.closing:
            alive = [n for n in _SERVICES if (p := self.procs.get(n)) and p.poll() is None]
            if len(alive) == len(_SERVICES):
                self.status_var.set("Running")
            elif alive:
                self.status_var.set("Running: " + ", ".join(alive))
            else:
                self.status_var.set("Stopped")
            self.root.after(1000, self._poll_status)

    def _drain_logs(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.logs.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            self.log_text.see("end")
            if int(self.log_text.index("end-1c").split(".")[0]) > 1000:
                self.log_text.delete("1.0", "200.0")
            self.log_text.configure(state="disabled")
        if not self.closing:
            self.root.after(250, self._drain_logs)

    # -- link helpers -----------------------------------------------------------------

    def open_url(self) -> None:
        webbrowser.open(self.url)

    def copy_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)

    def close(self) -> None:
        self.closing = True
        self.stop_all()
        self.root.destroy()


def main(child_command: Callable[[str], list[str]]) -> None:
    if not _acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        from tkinter import messagebox

        messagebox.showinfo("RecipeCollater", "RecipeCollater is already running.")
        root.destroy()
        return
    root = tk.Tk()
    SupervisorApp(root, child_command)
    root.mainloop()
