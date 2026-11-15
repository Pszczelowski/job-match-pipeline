"""The watchdog (app/watchdog.py, v0.80.6): H27 (missed schedule) and the clean-up after a run that was cut off.
Temporary databases and lock files, a fake SMTP; no network."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, cli, db, pipeline, watchdog
from app.config import load_config, resolve_path
from tests.test_report import PASSWORD, FakeSMTP

NOW = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"),
                      database=str(tmp_path / "t.db"))
    c["schedule"]["enabled"] = True
    c["email"]["retry_wait_seconds"] = 0
    return c


@pytest.fixture
def conn(tmp_path, cfg):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.failures = [], [], []


def go(cfg, **kw):
    return watchdog.run(cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=NOW, **kw)


def add_run(conn, run_id, status, started, finished=None):
    with conn:
        conn.execute("INSERT INTO runs (id, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
                     (run_id, stamp(started), stamp(finished) if finished else None, status))


def row(conn, key):
    return conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()


def write_lock(cfg, pid, pidns, started=NOW - timedelta(minutes=30), **extra):
    path = resolve_path(cfg["paths"]["lock"])
    path.write_text(json.dumps({"pid": pid, "started": stamp(started), "pidns": pidns, **extra}), encoding="utf-8")
    return path


# --- the clean-up ---------------------------------------------------------------------------------------------

def test_an_orphaned_run_is_marked_failed_and_alerted_and_no_lock_is_left(conn, cfg):
    add_run(conn, "run1", "running", NOW - timedelta(hours=3))
    assert go(cfg, cleanup=True) == 0
    r = conn.execute("SELECT status, finished_at, summary FROM runs WHERE id = 'run1'").fetchone()
    assert r["status"] == "failed" and r["finished_at"] and "found by the watchdog" in json.loads(r["summary"])["error"]
    a = row(conn, "run_interrupted:prepare")
    assert a is not None and a["resolved_at"] is None and "run1" in a["message"]
    assert not resolve_path(cfg["paths"]["lock"]).exists()
    assert [m["Subject"] for m in FakeSMTP.sent] == ["Recruitment alert: prepare was interrupted"]


def test_an_orphaned_run_behind_a_fresh_lock_of_another_container_is_left_alone(conn, cfg):
    add_run(conn, "run1", "running", NOW - timedelta(minutes=20))
    path = write_lock(cfg, 4242, "pid:[4026539999]")                # another container: its pid numbers mean nothing here
    before = path.read_text(encoding="utf-8")
    go(cfg, cleanup=True)
    assert conn.execute("SELECT status FROM runs WHERE id = 'run1'").fetchone()[0] == "running"
    assert path.read_text(encoding="utf-8") == before and row(conn, "run_interrupted:prepare") is None


def test_a_left_over_lock_and_its_orphaned_run_are_cleaned_with_a_faked_namespace(conn, cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "_pidns", lambda: "pid:[1]")
    monkeypatch.setattr(pipeline, "_pid_alive", lambda pid: False)
    add_run(conn, "run1", "running", NOW - timedelta(minutes=20))
    write_lock(cfg, 4242, "pid:[1]")
    go(cfg, cleanup=True)
    assert conn.execute("SELECT status FROM runs WHERE id = 'run1'").fetchone()[0] == "failed"
    assert row(conn, "run_interrupted:prepare") is not None and not resolve_path(cfg["paths"]["lock"]).exists()


@pytest.mark.skipif(pipeline._pidns() is None, reason="no readable PID namespace (not Linux)")
def test_a_lock_of_a_finished_process_in_our_namespace_is_cleaned(conn, cfg):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    add_run(conn, "run1", "running", NOW - timedelta(minutes=20))
    write_lock(cfg, proc.pid, pipeline._pidns())
    go(cfg, cleanup=True)
    assert conn.execute("SELECT status FROM runs WHERE id = 'run1'").fetchone()[0] == "failed"
    assert not resolve_path(cfg["paths"]["lock"]).exists()


def test_a_left_over_lock_without_an_orphaned_run_is_removed(conn, cfg):
    path = write_lock(cfg, 1, "pid:[1]", started=NOW - timedelta(hours=7))          # older than the 6 hours of the config
    go(cfg, cleanup=True)
    assert not path.exists() and row(conn, "run_interrupted:prepare") is None


def test_nothing_to_clean_takes_no_lock(conn, cfg, monkeypatch):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=2), NOW - timedelta(hours=1))

    def never(*a, **k):
        raise AssertionError("the lock must not be taken")
    monkeypatch.setattr(pipeline, "run_lock", never)
    assert go(cfg, cleanup=True) == 0
    assert not resolve_path(cfg["paths"]["lock"]).exists() and not list(resolve_path(cfg["paths"]["lock"]).parent.glob("*.tmp"))


def test_the_watchdog_never_raises(conn, cfg, monkeypatch):
    add_run(conn, "run1", "running", NOW - timedelta(hours=3))

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(watchdog, "check_missed_schedule", boom)
    assert go(cfg, cleanup=True) == 0
    cfg["paths"]["database"] = str(resolve_path(cfg["paths"]["database"]).parent / "missing.db")
    assert go(cfg, cleanup=True) == 0


# --- H27: the missed schedule ----------------------------------------------------------------------------------

def test_a_last_successful_prepare_over_30_hours_old_raises_the_alert_with_its_time(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=32), NOW - timedelta(hours=31))
    go(cfg)
    a = row(conn, alerts.MISSED_SCHEDULE_KEY)
    assert a["resolved_at"] is None and stamp(NOW - timedelta(hours=31)) in a["message"] and "31 hours" in a["message"]
    assert "heartbeat missing" in a["message"]


def test_a_last_successful_prepare_under_30_hours_old_clears_the_alert(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=32), NOW - timedelta(hours=31))
    go(cfg)
    add_run(conn, "run2", "ok", NOW - timedelta(hours=30), NOW - timedelta(hours=29))
    go(cfg)
    assert row(conn, alerts.MISSED_SCHEDULE_KEY)["resolved_at"] is not None


def test_a_failed_prepare_does_not_count_as_a_success(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=60), NOW - timedelta(hours=59))
    add_run(conn, "run2", "failed", NOW - timedelta(hours=2), NOW - timedelta(hours=1))
    go(cfg)
    assert row(conn, alerts.MISSED_SCHEDULE_KEY)["resolved_at"] is None


def test_the_schedule_off_raises_nothing_and_closes_an_open_alert(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=50), NOW - timedelta(hours=49))
    go(cfg)
    cfg["schedule"]["enabled"] = False
    go(cfg)
    assert row(conn, alerts.MISSED_SCHEDULE_KEY)["resolved_at"] is not None


def test_no_successful_prepare_on_record_raises_nothing(conn, cfg):
    add_run(conn, "run1", "failed", NOW - timedelta(hours=60), NOW - timedelta(hours=59))
    go(cfg)
    assert row(conn, alerts.MISSED_SCHEDULE_KEY) is None


def test_the_heartbeat_time_is_in_the_message(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=40), NOW - timedelta(hours=39))
    logs = resolve_path(cfg["paths"]["logs"])
    logs.mkdir(parents=True)
    (logs / "scheduler.heartbeat").write_text("x", encoding="ascii")
    os.utime(logs / "scheduler.heartbeat", (1_700_000_000, 1_700_000_000))
    go(cfg)
    assert "2023-11-14 22:13 UTC" in row(conn, alerts.MISSED_SCHEDULE_KEY)["message"]


# --- web mode (no --cleanup) -----------------------------------------------------------------------------------

def test_web_mode_writes_nothing_while_a_run_holds_the_lock(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=40), NOW - timedelta(hours=39))
    add_run(conn, "run2", "running", NOW - timedelta(minutes=10))
    with pipeline.run_lock(cfg, NOW - timedelta(minutes=10)):
        go(cfg)
    assert row(conn, alerts.MISSED_SCHEDULE_KEY) is None and FakeSMTP.sent == []
    assert conn.execute("SELECT status FROM runs WHERE id = 'run2'").fetchone()[0] == "running"


def test_web_mode_mails_only_the_missed_schedule_alert_and_never_cleans_up(conn, cfg):
    add_run(conn, "run1", "ok", NOW - timedelta(hours=40), NOW - timedelta(hours=39))
    add_run(conn, "run2", "running", NOW - timedelta(hours=3))
    alerts.observe(conn, cfg, "run_failed:finish", "an older problem", NOW - timedelta(days=2))
    go(cfg)
    assert [m["Subject"] for m in FakeSMTP.sent] == ["Recruitment alert: no successful prepare for more than 30 hours"]
    assert conn.execute("SELECT status FROM runs WHERE id = 'run2'").fetchone()[0] == "running"
    assert row(conn, "run_failed:finish")["last_sent"] is None


def test_the_cli_runs_the_watchdog_and_always_exits_0(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"paths:\n  database: {(tmp_path / 'data' / 'jobs.db').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n"
        f"  lock: {(tmp_path / 'data' / 'run.lock').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n",
        encoding="utf-8")
    import logging
    log = logging.getLogger("recruitment")
    try:
        assert cli.main(["--config", str(cfg_path), "watchdog", "--cleanup"]) == 0          # no database yet: nothing to do
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)


def test_cleanup_mails_only_its_own_alerts_not_the_daily_reminders(conn, cfg):
    add_run(conn, "run1", "running", NOW - timedelta(hours=3))
    alerts.observe(conn, cfg, "run_failed:finish", "an older problem", NOW - timedelta(days=2))
    go(cfg, cleanup=True)
    assert [m["Subject"] for m in FakeSMTP.sent] == ["Recruitment alert: prepare was interrupted"]
    assert row(conn, "run_failed:finish")["last_sent"] is None


def test_the_startup_cleanup_marks_a_run_of_an_earlier_container_start_failed(conn, cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "_pidns", lambda: "pid:[2]")
    add_run(conn, "run1", "running", NOW - timedelta(minutes=20))
    path = write_lock(cfg, 4242, "pid:[1]", scheduler=True)
    go(cfg, cleanup=True)                                          # a running scheduler: the lock may be another container's
    assert conn.execute("SELECT status FROM runs WHERE id = 'run1'").fetchone()[0] == "running" and path.exists()
    go(cfg, cleanup=True, startup=True)
    assert conn.execute("SELECT status FROM runs WHERE id = 'run1'").fetchone()[0] == "failed"
    assert row(conn, "run_interrupted:prepare") is not None and not path.exists()
