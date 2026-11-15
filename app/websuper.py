"""Slim web supervisor (goal.md 5.9a, v0.77.0): `python -m app web --supervise`.

Runs `python -m app web` as a child process and starts it again when it dies (backoff 5 s doubling to 60 s, reset after a
child ran for 60 s). A configuration error (exit code 3) or a clean exit (0) is not restarted. The state (running, restart
count, last exit code) is written to `web.supervisor` in paths.logs and shown by `status` as `web_supervisor`. No network.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import resolve_path

log = logging.getLogger("recruitment")

STATE_FILE = "web.supervisor"
BACKOFF_START = 5.0
BACKOFF_MAX = 60.0
STABLE_AFTER_SECONDS = 60.0
NO_RESTART_CODES = (0, 3)


def state_path(cfg: dict) -> Path:
    return resolve_path(cfg["paths"]["logs"]) / STATE_FILE


def write_state(cfg: dict, **state) -> None:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(state), encoding="ascii")


def read_state(cfg: dict) -> dict | None:
    """The supervisor's last state for `status`, or None when it never ran."""
    try:
        return json.loads(state_path(cfg).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def run_child(cmd: list[str], stop: threading.Event) -> int:
    proc = subprocess.Popen(cmd)
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if stop.is_set():
                proc.terminate()
                try:
                    return proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    return proc.wait()


def _watchdog_loop(watchdog: Callable[[], object], stop: threading.Event, every: float) -> None:
    while not stop.wait(every):
        try:
            watchdog()
        except Exception as exc:                                     # a failing check must not stop the supervisor
            log.error("web supervisor: the watchdog failed: %s", type(exc).__name__)


def supervise(cfg: dict, stop: threading.Event, child_args: list[str], *, child_tail: list[str] = (), child: Callable[[list[str]], int] | None = None,
              wait: Callable[[float], object] | None = None, clock: Callable[[], float] = time.monotonic,
              watchdog: Callable[[], object] | None = None, watchdog_every: float = 3600.0) -> int:
    """Start, watch and restart the web child. `child`, `wait` and `clock` are replaced in tests. Returns 0 when stopped.
    `watchdog` (v0.80.6) is called every `watchdog_every` seconds in a daemon thread: the missed-schedule check (H27) from the
    web container, which still runs when the scheduler container is down."""
    if watchdog is not None:
        threading.Thread(target=_watchdog_loop, args=(watchdog, stop, watchdog_every), daemon=True).start()
    cmd = [sys.executable, "-m", "app", *child_args, "web", *child_tail]
    run = child or (lambda c: run_child(c, stop))
    wait = wait or stop.wait
    restarts, delay = 0, BACKOFF_START
    while not stop.is_set():
        write_state(cfg, state="running", restarts=restarts, last_exit=None)
        started = clock()
        code = run(cmd)
        if stop.is_set():
            break
        log.warning("web supervisor: web app ended with exit code %s", code)
        if code in NO_RESTART_CODES:
            write_state(cfg, state="stopped", restarts=restarts, last_exit=code)
            return code
        restarts += 1
        if clock() - started >= STABLE_AFTER_SECONDS:
            delay = BACKOFF_START
        write_state(cfg, state="restarting", restarts=restarts, last_exit=code)
        wait(delay)
        delay = min(delay * 2, BACKOFF_MAX)
    write_state(cfg, state="stopped", restarts=restarts, last_exit=None)
    return 0
