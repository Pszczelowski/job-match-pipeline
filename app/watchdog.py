"""The watchdog (v0.80.6, goal.md 5.9): `python -m app watchdog [--cleanup]`.

Two jobs, both of which only record and mail, never decide anything about jobs:

* H27, the missed schedule: with `schedule.enabled`, when the last successful `prepare` finished more than
  `MISSED_SCHEDULE_HOURS` ago the alert `missed_schedule` is raised; otherwise it is cleared. The scheduler container runs it
  (`--cleanup`) and the web supervisor runs it hourly without `--cleanup`, so a container that is down or a loop that hangs
  is seen from the other container.
* The clean-up of a run that was cut off (`--cleanup`, scheduler only): a `runs` row still `running` is marked failed and
  raises `run_interrupted:prepare`; a left-over or unreadable run lock is replaced. Taking the lock keeps every other run
  out while this happens; a fresh lock of a live run is never touched.

It opens the database without a migration (also safe in the web container), never raises and always exits with 0.
Without `--cleanup` it does nothing while a run holds the lock (that run sends its own mails) and mails only `missed_schedule`.
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
from datetime import datetime, timezone

from . import alerts, db, pipeline, scheduler

log = logging.getLogger("recruitment")

MISSED_SCHEDULE_HOURS = 30
# the only alerts the watchdog mails: the daily reminders of every other alert keep coming with the daily run, not before it
OWN_KEYS = {alerts.MISSED_SCHEDULE_KEY, "run_interrupted:prepare", "run_interrupted:finish"}


def _cleanup(conn, cfg: dict, now: datetime, startup: bool = False) -> None:
    orphans = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
    state = pipeline.lock_status(cfg, now, startup)["state"]
    if not orphans and state not in ("left_over", "unreadable"):
        log.info("event=watchdog_cleanup cleaned=0 lock=%s", state)
        return
    stamp = now.strftime(alerts.STAMP)
    try:
        with pipeline.run_lock(cfg, now, startup):             # replaces a left-over lock and keeps every run out meanwhile
            rows = conn.execute("SELECT id, started_at FROM runs WHERE status = 'running' ORDER BY started_at, rowid").fetchall()
            for row in rows:
                with conn:
                    conn.execute("UPDATE runs SET finished_at = ?, status = 'failed', summary = ? WHERE id = ? AND status = 'running'",
                                 (db.utcnow(), json.dumps({"error": f"cut off (container stop, redeploy or crash); found by the watchdog at {stamp}"}),
                                  row["id"]))
                alerts.observe(conn, cfg, "run_interrupted:prepare",
                               f"run {row['id']} (started {row['started_at']}) was cut off (container stop, redeploy or crash) "
                               "and is marked failed", now)
                log.warning("event=watchdog_cleanup run=%s marked_failed=1", row["id"])
            log.info("event=watchdog_cleanup cleaned=%d lock=%s", len(rows), state)
    except pipeline.RunLockError as exc:
        log.info("event=watchdog_cleanup cleaned=0 reason=a_run_holds_the_lock detail=%s", exc)


def _heartbeat_text(cfg: dict) -> str:
    try:
        mtime = scheduler.heartbeat_path(cfg).stat().st_mtime
    except OSError:
        return "missing"
    return datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def check_missed_schedule(conn, cfg: dict, now: datetime) -> None:
    """H27. No successful prepare on record: nothing to compare, no alert. A failed prepare does not count (decided 11 Nov 2026)."""
    if not cfg["schedule"]["enabled"]:
        alerts.clear(conn, alerts.MISSED_SCHEDULE_KEY, now)
        return
    last = conn.execute("SELECT MAX(finished_at) FROM runs WHERE status = 'ok' AND finished_at IS NOT NULL").fetchone()[0]
    if not last:
        return
    hours = (now - datetime.strptime(last, alerts.STAMP).replace(tzinfo=timezone.utc)).total_seconds() / 3600
    if hours > MISSED_SCHEDULE_HOURS:
        alerts.observe(conn, cfg, alerts.MISSED_SCHEDULE_KEY,
                       f"the last successful prepare finished {last} UTC, {hours:.0f} hours ago; "
                       f"scheduler heartbeat {_heartbeat_text(cfg)}", now)
    else:
        alerts.clear(conn, alerts.MISSED_SCHEDULE_KEY, now)


def run(cfg: dict, *, cleanup: bool = False, startup: bool = False, now: datetime | None = None, smtp_factory=smtplib.SMTP, sleep=time.sleep) -> int:
    """The command. Always returns 0: a watchdog that fails must not look like a failed run."""
    now = now or datetime.now(timezone.utc)
    try:
        conn = alerts.open_without_migration(cfg)
        if conn is None:
            log.info("event=watchdog skipped=no_database")
            return 0
        try:
            if cleanup:
                _cleanup(conn, cfg, now, startup)
            elif pipeline.lock_is_held(cfg, now):
                log.info("event=watchdog skipped=a_run_holds_the_lock")
                return 0
            check_missed_schedule(conn, cfg, now)
            alerts.deliver(conn, cfg, smtp_factory=smtp_factory, sleep=sleep, now=now,
                           keys=OWN_KEYS if cleanup else {alerts.MISSED_SCHEDULE_KEY})
        finally:
            conn.close()
    except Exception as exc:                               # never raises: the watchdog is not a run
        log.error("event=watchdog failed=%s", type(exc).__name__)
    return 0
