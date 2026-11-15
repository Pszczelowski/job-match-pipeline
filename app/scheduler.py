"""The daily schedule inside the container (goal.md 5.9, 5.9a; Phase 5, v0.44.0).

A small loop in plain Python, started by `python -m app schedule`. Once a day at `schedule.prepare_time` (wall-clock time in
`timezone`, Europe/Zurich, so it follows daylight saving time) it runs `python -m app prepare` and then `python -m app
backup`, each as its own process (own exit code, own lock, own alerts, exactly as when the user types the command).
`finish` is never scheduled: it runs after the user has scored the batch (option C, goal.md 5.10). Exception (v0.78.0,
`headless.enabled: true`): the day is import-descriptions, prepare, backup, a request for the `llm` service (headless Claude
Code, app/headless.py), a wait for its done file and `finish --headless-done`; after a usage limit one more run follows
at the reset time (`headless.retry_*`), scheduled through the loop.

Off by default (`schedule.enabled: false`): the process then only idles and keeps its heartbeat, so the container stays
up and `docker compose exec` works. No root, no cron daemon, no extra package. No network access in this module.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import resolve_path
from .headless import RESUME_STAGES, RUN_ID_ENV, parse_reset

log = logging.getLogger("recruitment")

HEARTBEAT_FILE = "scheduler.heartbeat"          # in paths.logs; touched about every TICK_SECONDS
HEARTBEAT_MAX_AGE_SECONDS = 180                 # the container health check calls the loop dead after this
TICK_SECONDS = 20
STEP_TIMEOUT_SECONDS = 5 * 3600                 # below monitoring.lock_stale_hours (6)


def parse_time(value: str) -> tuple[int, int]:
    hour, minute = str(value).split(":")
    return int(hour), int(minute)


def next_run(after: datetime, hhmm: str, tz_name: str) -> datetime:
    """The first moment strictly after `after` at which the wall clock in `tz_name` shows `hhmm`; returned in UTC.

    Daylight saving time: a wall-clock time that does not exist (the hour skipped in March) is run at the moment the
    clock jumps over it plus the missing offset, so it runs once that day (02:30 becomes 03:30 summer time); a wall-clock
    time that occurs twice (the hour repeated in October) is run at its first occurrence only.
    """
    if after.tzinfo is None:
        raise ValueError("`after` must be timezone-aware")
    tz = ZoneInfo(tz_name)
    hour, minute = parse_time(hhmm)
    day = after.astimezone(tz).date()
    for _ in range(4):
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz, fold=0).astimezone(timezone.utc)
        if candidate > after:
            return candidate
        day += timedelta(days=1)
    raise RuntimeError("no next run found")        # cannot happen: one of the next two dates is later than `after`


def heartbeat_path(cfg: dict) -> Path:
    return resolve_path(cfg["paths"]["logs"]) / HEARTBEAT_FILE


def touch_heartbeat(cfg: dict) -> None:
    path = heartbeat_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="ascii")


_beat_failed = False


def _touch(cfg: dict) -> None:
    """`touch_heartbeat` that never raises: a full disk or a locked share must not end the loop (v0.80.6). One log line per
    streak of failures."""
    global _beat_failed
    try:
        touch_heartbeat(cfg)
        _beat_failed = False
    except OSError as exc:
        if not _beat_failed:
            log.error("event=heartbeat_failed error=%s", type(exc).__name__)
        _beat_failed = True


def health(cfg: dict, now: float | None = None) -> bool:
    """True when the loop wrote its heartbeat recently (used by the container health check)."""
    path = heartbeat_path(cfg)
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age <= HEARTBEAT_MAX_AGE_SECONDS


def run_step(name: str | list[str], stop: threading.Event, config_path: str | None = None, cfg: dict | None = None) -> int:
    """Run `python -m app <name>` as a child process; returns its exit code (-1 if stopped or timed out).
    `name` may be a list (a command with options, such as finish --headless-done FILE)."""
    cmd = [sys.executable, "-m", "app"] + (["--config", config_path] if config_path else []) + ([name] if isinstance(name, str) else list(name))
    proc = subprocess.Popen(cmd)                                                    # inherits environment and folder
    started = time.monotonic()
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if cfg is not None:                                                     # a long step must not make the health check fail
                try:
                    touch_heartbeat(cfg)
                except OSError:
                    pass
            if stop.is_set() or time.monotonic() - started > STEP_TIMEOUT_SECONDS:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return -1


@contextmanager
def run_id_environment(run_id: str):
    """The run id of one scheduled day (v0.78.2): made before the first step and set in the environment, so every step the
    scheduler starts (a child process inherits it) logs and stores the same id. The earlier value is put back afterwards."""
    before = os.environ.get(RUN_ID_ENV)
    os.environ[RUN_ID_ENV] = run_id
    try:
        yield run_id
    finally:
        if before is None:
            os.environ.pop(RUN_ID_ENV, None)
        else:
            os.environ[RUN_ID_ENV] = before


def run_day(cfg: dict, stop: threading.Event, step: Callable[[str], int], *,
            now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), wait: Callable[[float], object] | None = None,
            allow_retry: bool = True, outcome: dict | None = None) -> dict[str, int]:
    """prepare, then backup (also when prepare failed: the backup protects what is already stored).
    With `headless.enabled` the day is the headless day (`run_headless_day`); `outcome["retry_at"]` then holds the moment of the
    one retry after a usage limit, or None."""
    with run_id_environment(now().strftime("%Y-%m-%dT%H%M%SZ")):
        if cfg["headless"]["enabled"]:
            return run_headless_day(cfg, stop, step, now=now, wait=wait or stop.wait, allow_retry=allow_retry, outcome=outcome)
        results: dict[str, int] = {}
        steps = ["prepare"] + (["backup"] if cfg["schedule"]["run_backup"] else [])
        for name in steps:
            if stop.is_set():
                break
            log.info("event=step_start step=%s", name)
            results[name] = step(name)
            log.info("event=step_end step=%s rc=%s", name, results[name])
        return results


def _newest_batch(cfg: dict, since: float) -> Path | None:
    folder = resolve_path(cfg["paths"]["exchange"]) / "batches"
    found = [p for p in folder.glob("batch_*.json") if p.stat().st_mtime >= since] if folder.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _has_parked_run(cfg: dict) -> bool:
    """A run the worker left in a resumable stage (state.json in llm/work/<run>/)."""
    for state_file in (resolve_path(cfg["paths"]["exchange"]) / "llm" / "work").glob("*/state.json"):
        try:
            if json.loads(state_file.read_text(encoding="utf-8")).get("stage") in RESUME_STAGES:
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def write_request(cfg: dict, run_id: str, with_batch: bool = True, deadline: datetime | None = None) -> Path:
    """The request for the llm service. `deadline` (UTC) is the moment the scheduler stops waiting; the worker starts no role
    that would not end 15 minutes before it (audit A5)."""
    hl = cfg["headless"]
    path = resolve_path(cfg["paths"]["exchange"]) / "llm" / "requests" / f"{run_id}.json"
    _write_json(path, {"run_id": run_id, "batch_file": f"batches/batch_{run_id}.json" if with_batch else None, "roles": hl["roles"],
                       "resume_attempts": hl["resume_attempts"],
                       **({"deadline": deadline.astimezone(timezone.utc).isoformat(timespec="seconds")} if deadline else {})})
    return path


def wait_for_done(cfg: dict, run_id: str, stop: threading.Event, now: Callable[[], datetime], wait: Callable[[float], object],
                  deadline: datetime | None = None) -> dict:
    """Wait for exchange/llm/done/<run_id>.json until `deadline` (default: headless.max_wait_minutes from now); the heartbeat is
    touched on every look. When `stop` is set before the deadline nothing is written and {"status": "stopped"} returns. At the timeout a cancel marker and a synthetic done file {"status": "timeout"} are written."""
    hl = cfg["headless"]
    llm = resolve_path(cfg["paths"]["exchange"]) / "llm"
    done_path = llm / "done" / f"{run_id}.json"
    deadline = deadline or now() + timedelta(minutes=hl["max_wait_minutes"])
    while not stop.is_set():
        _touch(cfg)
        if done_path.exists():
            try:
                data = json.loads(done_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {"status": "error", "message": "the done file is not an object"}
            except (OSError, ValueError):
                return {"status": "error", "message": "the done file cannot be read"}
        if now() >= deadline:
            break
        wait(float(hl["poll_seconds"]))
    if stop.is_set() and now() < deadline:                          # stopped while waiting: nothing is cancelled, no done file
        return {"run_id": run_id, "status": "stopped", "message": "the scheduler stopped while waiting"}
    marker = llm / "cancel" / run_id
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("cancel", encoding="ascii")
    synthetic = {"run_id": run_id, "status": "timeout", "message": "the llm service did not answer in time"}
    done_path.parent.mkdir(parents=True, exist_ok=True)
    try:                                                            # exclusive: a done file that just arrived is not replaced
        with done_path.open("x", encoding="utf-8") as fh:
            json.dump(synthetic, fh, ensure_ascii=False, indent=1)
    except FileExistsError:
        try:
            data = json.loads(done_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"status": "error", "message": "the done file is not an object"}
        except (OSError, ValueError):
            return {"status": "error", "message": "the done file cannot be read"}
    return synthetic


def limit_message_of(done: dict) -> str | None:
    """The limit message of a done file whose own run or a resumed run hit the usage limit; None when none did."""
    parts = [done] + [r for r in done.get("resumed", []) if isinstance(r, dict)]
    for part in parts:
        if part.get("status") == "usage_limit":
            return str(part.get("limit_message") or "")
    return None


def retry_moment(cfg: dict, done: dict, current: datetime) -> datetime | None:
    """B3: after a usage limit, when the one retry runs: the reset time read from the limit message (one minute later), else
    `retry_fallback_minutes` after `current`. None when the reset is not today or later than `headless.retry_latest`."""
    hl = cfg["headless"]
    if not hl["retry_after_limit"]:
        return None
    message = limit_message_of(done)
    if message is None:
        return None
    reset = parse_reset(str(message), current, cfg["timezone"]) or current + timedelta(minutes=hl["retry_fallback_minutes"])
    tz = ZoneInfo(cfg["timezone"])
    local, today = reset.astimezone(tz), current.astimezone(tz)
    hour, minute = parse_time(hl["retry_latest"])
    if local.date() != today.date() or (local.hour, local.minute) > (hour, minute):
        return None
    return reset.astimezone(timezone.utc) + timedelta(minutes=1)


def run_headless_day(cfg: dict, stop: threading.Event, step: Callable, *, now: Callable[[], datetime],
                     wait: Callable[[float], object], allow_retry: bool = True, outcome: dict | None = None) -> dict[str, int]:
    """import-descriptions, prepare, backup, the request, the wait, finish --headless-done (goal.md 5.10, v0.78.0)."""
    results: dict[str, int] = {}
    outcome = outcome if outcome is not None else {}
    outcome["retry_at"] = None
    started = time.time() - 2                                       # file times have a coarse resolution
    for name in ["import-descriptions", "prepare"] + (["backup"] if cfg["schedule"]["run_backup"] else []):
        if stop.is_set():
            return results
        log.info("event=step_start step=%s", name)
        results[name] = step(name)
        log.info("event=step_end step=%s rc=%s", name, results[name])
    if stop.is_set():
        return results
    batch = _newest_batch(cfg, started)
    parked = batch is None and _has_parked_run(cfg)                 # no new batch, but a run waits for its letters or review
    done_path: Path | None = None
    done: dict | None = None
    if batch is not None or parked:
        run_id = batch.stem[len("batch_"):] if batch is not None else os.environ.get(RUN_ID_ENV) or now().strftime("%Y-%m-%dT%H%M%SZ")
        if batch is not None and run_id != os.environ.get(RUN_ID_ENV):
            log.warning("event=run_id_mismatch batch=%s", run_id)
        deadline = now() + timedelta(minutes=cfg["headless"]["max_wait_minutes"])     # one deadline for the request and the wait
        write_request(cfg, run_id, with_batch=batch is not None, deadline=deadline)
        log.info("event=request_written request=%s", run_id)
        done = wait_for_done(cfg, run_id, stop, now, wait, deadline)
        done_path = resolve_path(cfg["paths"]["exchange"]) / "llm" / "done" / f"{run_id}.json"
        log.info("event=llm_answered status=%s message=%s", done.get("status"), " ".join(str(done.get("message") or "-").split())[:300])
    else:
        log.info("event=no_batch nothing to score")
    if stop.is_set():
        return results
    results["finish"] = step(["finish", "--headless-done", str(done_path)] if done_path else ["finish"])
    log.info("event=step_end step=finish rc=%s", results["finish"])
    if allow_retry and done is not None:
        outcome["retry_at"] = retry_moment(cfg, done, now())
        if outcome["retry_at"]:
            log.info("event=usage_limit_retry at=%sZ", outcome["retry_at"].strftime("%Y-%m-%dT%H:%M"))
    return results


def loop(cfg: dict, stop: threading.Event, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
         step: Callable[[str], int] | None = None, wait: Callable[[float], object] | None = None,
         max_ticks: int | None = None, watchdog: Callable[[], int] | None = None,
         watchdog_startup: Callable[[], int] | None = None, revision_check: Callable[[dict, datetime, bool], bool] | None = None) -> int:
    """The scheduler loop. `now`, `step` and `wait` are replaced in tests. Returns 0 when stopped. `watchdog` (v0.80.6,
    `watchdog --cleanup`) runs once at the start, before each daily run and after a day in which a step was cut off (rc below 0)
    while the loop is not being stopped. With `headless.enabled` an idle tick looks for revision work (`revision_check`, default
    `revisions.has_work`) and runs the step `revisions`, with `--no-relay` in the quiet window before the next daily run (v0.85.0)."""
    sched = cfg["schedule"]
    wait = wait or stop.wait
    if step is None:
        def step(name: str) -> int:
            return run_step(name, stop, cfg=cfg)
    due = next_run(now(), sched["prepare_time"], cfg["timezone"]) if sched["enabled"] else None
    if due:
        log.info("scheduler: on; prepare every day at %s %s, first run %s UTC", sched["prepare_time"], cfg["timezone"],
                 due.strftime("%Y-%m-%d %H:%M"))
    else:
        log.info("scheduler: off (schedule.enabled is false); idling so that 'docker compose exec' works")
    ticks = 0
    retry_at: datetime | None = None

    def run_watchdog(startup: bool = False) -> None:
        """The watchdog never ends the loop: an error (a process that cannot be started) is logged. At the start of the loop
        (`startup`) `watchdog_startup` runs: a lock of another PID namespace is a leftover of the stopped container."""
        call = (watchdog_startup or watchdog) if startup else watchdog
        if call is None:
            return
        try:
            call()
        except Exception as exc:
            log.error("event=watchdog_failed error=%s", type(exc).__name__)

    def guarded_day(**kw) -> dict[str, int]:
        """One day; an OSError (a full disk, a locked share) is logged and the loop goes on with the next due time."""
        try:
            results = run_day(cfg, stop, step, now=now, wait=wait, **kw)
        except OSError as exc:
            log.error("event=day_failed error=%s", type(exc).__name__)
            return {}
        if watchdog is not None and not stop.is_set() and any(rc < 0 for rc in results.values()):
            run_watchdog()                                          # a step was cut off: clean up what it left behind
        return results

    def run_revisions() -> None:
        """"Ask the writer to fix" (v0.85.0): only when there is work, so an idle tick starts no process. Never ends the loop."""
        quiet_seconds = cfg["headless"]["revision"]["quiet_minutes"] * 60
        quiet = any(t is not None and (t - now()).total_seconds() < quiet_seconds for t in (due, retry_at))
        try:
            check = revision_check
            if check is None:
                from .revisions import has_work as check
            if not check(cfg, now(), not quiet):
                return
            rc = step(["revisions"] + (["--no-relay"] if quiet else []))
            log.info("event=step_end step=revisions rc=%s", rc)
        except Exception as exc:
            log.error("event=revisions_failed error=%s", type(exc).__name__)

    if not stop.is_set():
        run_watchdog(startup=True)
    while not stop.is_set():
        _touch(cfg)
        if due is not None and now() >= due:
            outcome: dict = {}
            run_watchdog()
            guarded_day(outcome=outcome)
            retry_at = outcome.get("retry_at")
            due = next_run(now(), sched["prepare_time"], cfg["timezone"])
            log.info("scheduler: next prepare %s UTC", due.strftime("%Y-%m-%d %H:%M"))
        elif retry_at is not None and now() >= retry_at:
            retry_at = None                                         # one retry only (B3)
            guarded_day(allow_retry=False)
        if cfg["headless"]["enabled"] and not stop.is_set():
            run_revisions()
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        target = min([d for d in (due, retry_at) if d is not None], default=None)
        remaining = (target - now()).total_seconds() if target is not None else TICK_SECONDS
        wait(max(1.0, min(TICK_SECONDS, remaining)))
    return 0


def command(cfg: dict, *, health_only: bool = False, show_next: bool = False) -> int:
    """`python -m app schedule [--health] [--next]`."""
    sched = cfg["schedule"]
    if health_only:
        ok = health(cfg)
        print("ok" if ok else "scheduler heartbeat missing or too old")
        return 0 if ok else 1
    if show_next:
        if not sched["enabled"]:
            print(f"scheduler is off (schedule.enabled is false); it would run prepare at {sched['prepare_time']} {cfg['timezone']}")
            return 0
        due = next_run(datetime.now(timezone.utc), sched["prepare_time"], cfg["timezone"])
        local = due.astimezone(ZoneInfo(cfg["timezone"]))
        print(f"next prepare: {local.strftime('%Y-%m-%d %H:%M %Z')} ({due.strftime('%Y-%m-%d %H:%M')} UTC)")
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return loop(cfg, stop, watchdog=lambda: run_step(["watchdog", "--cleanup"], stop, cfg=cfg),
                watchdog_startup=lambda: run_step(["watchdog", "--cleanup", "--startup"], stop, cfg=cfg))
