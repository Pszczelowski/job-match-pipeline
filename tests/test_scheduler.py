"""The container scheduler (app/scheduler.py): time computation across daylight saving time, the loop, the command.
No network, no real child process, no real clock."""
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import cli, scheduler
from app.config import ConfigError, load_config

ZH = ZoneInfo("Europe/Zurich")
UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def local(dt):
    return dt.astimezone(ZH).strftime("%Y-%m-%d %H:%M %Z")


def cfg_for(tmp_path, **schedule):
    cfg = load_config()
    cfg["paths"]["logs"] = str(tmp_path / "logs")
    cfg["schedule"].update(schedule)
    return cfg


# ---- next_run ----------------------------------------------------------------------------------------------------

def test_next_run_later_today_and_tomorrow():
    assert local(scheduler.next_run(utc(2026, 11, 2, 4, 0), "06:30", "Europe/Zurich")) == "2026-11-02 06:30 CET"      # 05:00 local
    assert local(scheduler.next_run(utc(2026, 11, 2, 6, 0), "06:30", "Europe/Zurich")) == "2026-11-03 06:30 CET"      # 07:00 local


def test_next_run_is_strictly_after_the_given_moment():
    due = scheduler.next_run(utc(2026, 11, 2, 4, 0), "06:30", "Europe/Zurich")
    assert scheduler.next_run(due, "06:30", "Europe/Zurich") == due + timedelta(days=1)


def test_next_run_is_utc_and_follows_summer_and_winter_time():
    summer = scheduler.next_run(utc(2026, 7, 1, 0, 0), "06:30", "Europe/Zurich")
    winter = scheduler.next_run(utc(2026, 12, 1, 0, 0), "06:30", "Europe/Zurich")
    assert summer.tzinfo is not None and summer.utcoffset() == timedelta(0)
    assert (summer.hour, summer.minute) == (4, 30)                                       # CEST = UTC+2
    assert (winter.hour, winter.minute) == (5, 30)                                       # CET = UTC+1


def test_next_run_across_the_spring_change_keeps_the_wall_clock_time():
    # 2027-03-28 is the day the clocks jump from 02:00 to 03:00. 06:30 stays 06:30 local.
    after_change = utc(2027, 3, 27, 6, 0)                                                # 07:00 CET on the 27th
    assert local(scheduler.next_run(after_change, "06:30", "Europe/Zurich")) == "2027-03-28 06:30 CEST"
    run_27 = scheduler.next_run(utc(2027, 3, 26, 12, 0), "06:30", "Europe/Zurich")
    run_28 = scheduler.next_run(run_27, "06:30", "Europe/Zurich")
    assert run_28 - run_27 == timedelta(hours=23)                                        # a 23-hour day


def test_next_run_across_the_autumn_change_keeps_the_wall_clock_time():
    # 2026-10-25: clocks go back from 03:00 to 02:00, a 25-hour day.
    run_24 = scheduler.next_run(utc(2026, 10, 23, 12, 0), "06:30", "Europe/Zurich")
    run_25 = scheduler.next_run(run_24, "06:30", "Europe/Zurich")
    run_26 = scheduler.next_run(run_25, "06:30", "Europe/Zurich")
    assert [local(r) for r in (run_24, run_25, run_26)] == ["2026-10-24 06:30 CEST", "2026-10-25 06:30 CET", "2026-10-26 06:30 CET"]
    assert run_26 - run_25 == timedelta(hours=24)
    assert run_25 - run_24 == timedelta(hours=25)


def test_a_time_that_does_not_exist_runs_once_after_the_jump():
    # 02:30 does not exist on 2027-03-28; it runs at the moment 02:30 CET would have been, which is 03:30 CEST
    run = scheduler.next_run(utc(2027, 3, 27, 12, 0), "02:30", "Europe/Zurich")
    assert local(run) == "2027-03-28 03:30 CEST"
    assert local(scheduler.next_run(run, "02:30", "Europe/Zurich")) == "2027-03-29 02:30 CEST"                       # not twice


def test_a_time_that_occurs_twice_runs_once_at_its_first_occurrence():
    # 02:30 happens twice on 2026-10-25 (CEST, then CET); only the first one counts
    first = scheduler.next_run(utc(2026, 10, 24, 12, 0), "02:30", "Europe/Zurich")
    assert local(first) == "2026-10-25 02:30 CEST"
    after_second = scheduler.next_run(first, "02:30", "Europe/Zurich")
    assert local(after_second) == "2026-10-26 02:30 CET"                                  # the repeated 02:30 CET is skipped
    assert after_second - first == timedelta(hours=25)


def test_next_run_needs_an_aware_datetime():
    with pytest.raises(ValueError):
        scheduler.next_run(datetime(2026, 11, 2, 4, 0), "06:30", "Europe/Zurich")


# ---- config ------------------------------------------------------------------------------------------------------

def test_defaults_are_off(tmp_path):
    cfg = load_config()
    assert cfg["schedule"] == {"enabled": False, "prepare_time": "06:30", "run_backup": True}


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"prepare_time": "6:30"}, {"prepare_time": "24:00"}, {"prepare_time": 630},
                                 {"run_backup": 1}])
def test_bad_schedule_values_are_refused(tmp_path, bad):
    path = tmp_path / "c.yaml"
    path.write_text("schedule:\n" + "".join(f"  {k}: {v!r}\n" for k, v in bad.items()), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_time_zone_is_refused(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("timezone: Mars/Olympus\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="time zone"):
        load_config(path)


# ---- the loop ----------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, start):
        self.t = start
        self.waits = []

    def now(self):
        return self.t

    def wait(self, seconds):
        self.waits.append(seconds)
        self.t += timedelta(seconds=seconds)


def test_loop_off_never_runs_a_step_but_keeps_the_heartbeat(tmp_path):
    cfg = cfg_for(tmp_path, enabled=False)
    clock, calls = Clock(utc(2026, 11, 2, 5, 0)), []
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=500)
    assert calls == []
    assert scheduler.heartbeat_path(cfg).exists()
    assert scheduler.health(cfg)


def test_loop_runs_prepare_then_backup_once_a_day(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls = Clock(utc(2026, 11, 2, 4, 0)), []            # 05:00 local
    # 3 days at 20-second ticks
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append((local(clock.now()), n)) or 0,
                   wait=clock.wait, max_ticks=3 * 24 * 180)
    names = [n for _, n in calls]
    assert names[:4] == ["prepare", "backup", "prepare", "backup"]
    assert [t for t, n in calls if n == "prepare"][:2] == ["2026-11-02 06:30 CET", "2026-11-03 06:30 CET"]


def test_loop_without_backup_step(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, run_backup=False)
    clock, calls = Clock(utc(2026, 11, 2, 4, 0)), []
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=5 * 180)
    assert calls == ["prepare"]


def test_backup_runs_even_when_prepare_failed(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True)
    codes = {"prepare": 1, "backup": 0}
    results = scheduler.run_day(cfg, threading.Event(), lambda n: codes[n])
    assert results == codes


def test_nothing_runs_when_stopped_between_steps(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True)
    stop, calls = threading.Event(), []

    def step(name):
        calls.append(name)
        stop.set()
        return 0
    scheduler.run_day(cfg, stop, step)
    assert calls == ["prepare"]


def test_loop_stops_at_once_when_the_event_is_set(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True)
    stop = threading.Event()
    stop.set()
    assert scheduler.loop(cfg, stop, now=lambda: utc(2026, 11, 2, 4, 0), step=lambda n: 0, wait=lambda s: None) == 0


def test_wait_never_exceeds_one_tick(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True)
    clock = Clock(utc(2026, 11, 2, 4, 0))
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: 0, wait=clock.wait, max_ticks=20)
    assert max(clock.waits) <= scheduler.TICK_SECONDS and min(clock.waits) >= 1


# ---- heartbeat, command ------------------------------------------------------------------------------------------

def test_health_needs_a_fresh_heartbeat(tmp_path):
    cfg = cfg_for(tmp_path)
    assert scheduler.health(cfg) is False                                                  # no file
    scheduler.touch_heartbeat(cfg)
    mtime = scheduler.heartbeat_path(cfg).stat().st_mtime
    assert scheduler.health(cfg, now=mtime + 60) is True
    assert scheduler.health(cfg, now=mtime + scheduler.HEARTBEAT_MAX_AGE_SECONDS + 1) is False


def test_command_health_and_next(tmp_path, capsys):
    cfg = cfg_for(tmp_path)
    assert scheduler.command(cfg, health_only=True) == 1
    scheduler.touch_heartbeat(cfg)
    assert scheduler.command(cfg, health_only=True) == 0
    assert scheduler.command(cfg, show_next=True) == 0
    assert "off" in capsys.readouterr().out
    cfg["schedule"]["enabled"] = True
    assert scheduler.command(cfg, show_next=True) == 0
    assert "next prepare:" in capsys.readouterr().out


def test_schedule_command_is_wired_in_the_cli(tmp_path, capsys):
    config = tmp_path / "c.yaml"
    config.write_text(f"paths:\n  logs: {(tmp_path / 'logs').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "schedule", "--next"]) == 0
    assert "scheduler is off" in capsys.readouterr().out
    assert cli.main(["--config", str(config), "schedule", "--health"]) == 1               # no heartbeat yet


def test_run_step_command_line_runs_the_app_module(monkeypatch):
    seen = {}

    class FakeProc:
        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()
    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)
    assert scheduler.run_step("prepare", threading.Event()) == 0
    assert seen["cmd"][1:] == ["-m", "app", "prepare"]
    assert scheduler.run_step("backup", threading.Event(), "x.yaml") == 0
    assert seen["cmd"][1:] == ["-m", "app", "--config", "x.yaml", "backup"]
# ---- headless day (v0.78.0, goal.md 5.10) -------------------------------------------------------------------------

LIMIT = "You've hit your session limit · resets 3pm (Europe/Zurich)"


def headless_cfg(tmp_path, **headless):
    cfg = cfg_for(tmp_path, enabled=True)
    cfg["paths"]["exchange"] = str(tmp_path / "exchange")
    cfg["headless"]["enabled"] = True
    cfg["headless"].update(headless)
    return cfg


def llm_dir(cfg):
    return scheduler.resolve_path(cfg["paths"]["exchange"]) / "llm"


class FakeDay:
    """A step that records its name, writes a batch file on `prepare` and remembers when each step ran."""

    def __init__(self, cfg, write_batch=True, clock=None):
        self.cfg, self.calls, self.write_batch, self.n, self.clock, self.times = cfg, [], write_batch, 0, clock, []

    def __call__(self, name):
        self.calls.append(name)
        if self.clock:
            self.times.append((local(self.clock.now()), name if isinstance(name, str) else name[0]))
        if name == "prepare" and self.write_batch:
            self.n += 1
            folder = scheduler.resolve_path(self.cfg["paths"]["exchange"]) / "batches"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"batch_run{self.n}.json").write_text("{}", encoding="utf-8")
        return 0


def answer_with(cfg, status="ok", **extra):
    """A `wait` that, on its first call, plays the llm service: the done file of the newest request appears."""
    seen = []

    def wait(seconds):
        seen.append(seconds)
        if len(seen) == 1:
            run_id = sorted((llm_dir(cfg) / "requests").glob("*.json"))[-1].stem
            scheduler._write_json(llm_dir(cfg) / "done" / f"{run_id}.json", {"run_id": run_id, "status": status, **extra})
    return wait


def test_run_day_with_headless_on_runs_finish_after_prepare(tmp_path):
    cfg = headless_cfg(tmp_path)
    step = FakeDay(cfg)
    results = scheduler.run_day(cfg, threading.Event(), step, now=lambda: utc(2026, 11, 2, 5, 0), wait=answer_with(cfg))
    assert [c if isinstance(c, str) else c[0] for c in step.calls] == ["import-descriptions", "prepare", "backup", "finish"]
    assert step.calls[-1] == ["finish", "--headless-done", str(llm_dir(cfg) / "done" / "run1.json")]
    request = json.loads((llm_dir(cfg) / "requests" / "run1.json").read_text(encoding="utf-8"))
    assert request["run_id"] == "run1" and request["batch_file"] == "batches/batch_run1.json"
    assert request["roles"] == cfg["headless"]["roles"] and results["finish"] == 0


def test_headless_off_writes_no_request_and_runs_no_finish(tmp_path):
    cfg = headless_cfg(tmp_path, enabled=False)
    step = FakeDay(cfg)
    scheduler.run_day(cfg, threading.Event(), step)
    assert step.calls == ["prepare", "backup"]
    assert not (tmp_path / "exchange" / "llm").exists()


def test_headless_day_without_a_new_batch_sends_no_request_but_still_finishes(tmp_path):
    cfg = headless_cfg(tmp_path)
    step = FakeDay(cfg, write_batch=False)
    scheduler.run_day(cfg, threading.Event(), step, now=lambda: utc(2026, 11, 2, 5, 0), wait=lambda s: pytest.fail("must not wait"))
    assert step.calls == ["import-descriptions", "prepare", "backup", ["finish"]]
    assert not (tmp_path / "exchange" / "llm").exists()


def park_run(cfg, stage="letters", run_id="old1"):
    work = llm_dir(cfg) / "work" / run_id
    work.mkdir(parents=True)
    (work / "state.json").write_text(json.dumps({"run_id": run_id, "stage": stage, "attempts": 0}), encoding="utf-8")


def test_a_parked_run_without_a_new_batch_gets_a_request_without_a_batch_file(tmp_path):
    cfg = headless_cfg(tmp_path)
    park_run(cfg)
    step = FakeDay(cfg, write_batch=False)
    scheduler.run_day(cfg, threading.Event(), step, now=lambda: utc(2026, 11, 2, 5, 0), wait=answer_with(cfg))
    request = json.loads(next((llm_dir(cfg) / "requests").glob("*.json")).read_text(encoding="utf-8"))
    assert request["batch_file"] is None and request["roles"] == cfg["headless"]["roles"]
    assert step.calls[-1][:2] == ["finish", "--headless-done"]


def test_a_finished_or_failed_run_in_the_work_folder_does_not_count_as_parked(tmp_path):
    cfg = headless_cfg(tmp_path)
    park_run(cfg, "done", "old1")
    park_run(cfg, "failed", "old2")
    step = FakeDay(cfg, write_batch=False)
    scheduler.run_day(cfg, threading.Event(), step, now=lambda: utc(2026, 11, 2, 5, 0), wait=lambda s: pytest.fail("must not wait"))
    assert step.calls[-1] == ["finish"] and not (llm_dir(cfg) / "requests").exists()


def test_the_timeout_does_not_overwrite_a_done_file_that_arrived_at_the_deadline(tmp_path):
    cfg = headless_cfg(tmp_path)
    done_path = llm_dir(cfg) / "done" / "run1.json"
    start = utc(2026, 11, 2, 4, 0)
    calls = []

    def now():
        calls.append(1)
        if len(calls) == 2:                                          # the deadline check: the worker answers just before the timeout
            scheduler._write_json(done_path, {"run_id": "run1", "status": "ok"})
            return start + timedelta(hours=9)
        return start
    done = scheduler.wait_for_done(cfg, "run1", threading.Event(), now, lambda s: None)
    assert done["status"] == "ok" and json.loads(done_path.read_text(encoding="utf-8"))["status"] == "ok"


def test_run_step_touches_the_heartbeat_while_the_child_runs(tmp_path, monkeypatch):
    cfg = headless_cfg(tmp_path)

    class SlowProc:
        n = 0

        def wait(self, timeout=None):
            SlowProc.n += 1
            if SlowProc.n <= 3:
                raise subprocess.TimeoutExpired("x", timeout)
            return 0
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda cmd, **kw: SlowProc())
    beats = []
    monkeypatch.setattr(scheduler, "touch_heartbeat", lambda c: beats.append(c))
    assert scheduler.run_step("prepare", threading.Event(), cfg=cfg) == 0
    assert len(beats) == 3


def test_wait_timeout_writes_cancel_and_a_synthetic_done_file_and_finish_still_runs(tmp_path):
    cfg = headless_cfg(tmp_path, max_wait_minutes=275)
    clock, step = Clock(utc(2026, 11, 2, 4, 0)), FakeDay(cfg)
    scheduler.run_day(cfg, threading.Event(), step, now=clock.now, wait=clock.wait)
    assert (llm_dir(cfg) / "cancel" / "run1").exists()
    assert json.loads((llm_dir(cfg) / "done" / "run1.json").read_text(encoding="utf-8"))["status"] == "timeout"
    assert step.calls[-1][:2] == ["finish", "--headless-done"]
    assert 275 * 60 - cfg["headless"]["poll_seconds"] <= sum(clock.waits) <= 275 * 60 + cfg["headless"]["poll_seconds"]


def test_the_request_carries_the_deadline_the_scheduler_waits_for(tmp_path):
    cfg = headless_cfg(tmp_path, max_wait_minutes=270)
    clock, step = Clock(utc(2026, 11, 2, 4, 0)), FakeDay(cfg)
    scheduler.run_day(cfg, threading.Event(), step, now=clock.now, wait=clock.wait)
    request = json.loads((llm_dir(cfg) / "requests" / "run1.json").read_text(encoding="utf-8"))
    assert request["deadline"] == "2026-11-02T08:30:00+00:00"
    assert (llm_dir(cfg) / "cancel" / "run1").exists()                               # the wait ended at that same moment
    assert 270 * 60 - cfg["headless"]["poll_seconds"] <= sum(clock.waits) <= 270 * 60 + cfg["headless"]["poll_seconds"]


def test_the_heartbeat_is_touched_on_every_look_while_waiting(tmp_path, monkeypatch):
    cfg = headless_cfg(tmp_path)
    clock, step, beats = Clock(utc(2026, 11, 2, 4, 0)), FakeDay(cfg), []
    real = scheduler.touch_heartbeat
    monkeypatch.setattr(scheduler, "touch_heartbeat", lambda c: beats.append(1) or real(c))
    scheduler.run_day(cfg, threading.Event(), step, now=clock.now, wait=clock.wait)
    assert len(clock.waits) > 500 and len(beats) >= len(clock.waits)             # a look before every wait
    assert scheduler.health(cfg)


def limit_loop(tmp_path, message, **headless):
    """Two days of the loop; the llm service answers every request with a usage limit."""
    cfg = headless_cfg(tmp_path, **headless)
    clock = Clock(utc(2026, 11, 2, 4, 0))
    step = FakeDay(cfg, clock=clock)

    def wait(seconds):
        clock.wait(seconds)
        for req in sorted((llm_dir(cfg) / "requests").glob("*.json")):
            done = llm_dir(cfg) / "done" / req.name
            if not done.exists():
                scheduler._write_json(done, {"run_id": req.stem, "status": "usage_limit", "limit_message": message})
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=step, wait=wait, max_ticks=2 * 24 * 180)
    return [t for t in step.times if t[1] == "prepare"]


def test_usage_limit_retries_once_at_the_reset_time_and_a_second_limit_does_not_retry_again(tmp_path):
    prepares = limit_loop(tmp_path, LIMIT)
    # day 1 06:30, the one retry at 15:01 (3pm + 1 minute), then day 2 at 06:30; the retry's own limit starts no third run
    assert [t for t, _ in prepares][:4] == ["2026-11-02 06:30 CET", "2026-11-02 15:01 CET", "2026-11-03 06:30 CET", "2026-11-03 15:01 CET"]


def test_no_retry_when_the_reset_is_after_the_latest_time(tmp_path):
    prepares = limit_loop(tmp_path, "You've hit your session limit · resets 9pm (Europe/Zurich)")
    assert [t for t, _ in prepares][:2] == ["2026-11-02 06:30 CET", "2026-11-03 06:30 CET"]


def test_retry_after_limit_false_runs_no_retry(tmp_path):
    prepares = limit_loop(tmp_path, LIMIT, retry_after_limit=False)
    assert [t for t, _ in prepares][:2] == ["2026-11-02 06:30 CET", "2026-11-03 06:30 CET"]


def test_retry_moment_is_the_reset_plus_one_minute(tmp_path):
    cfg = headless_cfg(tmp_path)
    when = scheduler.retry_moment(cfg, {"status": "usage_limit", "limit_message": LIMIT}, utc(2026, 11, 2, 5, 40))
    assert local(when) == "2026-11-02 15:01 CET"


def test_no_retry_when_the_reset_is_tomorrow(tmp_path):
    cfg = headless_cfg(tmp_path)
    done = {"status": "usage_limit", "limit_message": "hit your session limit · resets 6am (Europe/Zurich)"}
    assert scheduler.retry_moment(cfg, done, utc(2026, 11, 2, 5, 40)) is None


def test_unreadable_reset_time_retries_after_the_fallback_wait(tmp_path):
    cfg = headless_cfg(tmp_path)
    done = {"status": "usage_limit", "limit_message": "hit your session limit"}
    assert local(scheduler.retry_moment(cfg, done, utc(2026, 11, 2, 5, 0))) == "2026-11-02 11:01 CET"     # 06:00 local + 5 h
    assert scheduler.retry_moment(cfg, done, utc(2026, 11, 2, 15, 0)) is None                              # 16:00 + 5 h is after 20:00


def test_no_retry_for_other_statuses(tmp_path):
    cfg = headless_cfg(tmp_path)
    for status in ("ok", "error", "auth", "timeout", "partial"):
        assert scheduler.retry_moment(cfg, {"status": status, "limit_message": "resets 3pm"}, utc(2026, 11, 2, 5, 0)) is None


@pytest.mark.parametrize("text, expected", [("resets 3pm (Europe/Zurich)", "2026-11-02 15:00"), ("resets 3:30pm", "2026-11-02 15:30"),
                                             ("resets 11am (Europe/Zurich)", "2026-11-02 11:00"), ("resets 12am", "2026-11-03 00:00"),
                                             ("resets 14:05", "2026-11-02 14:05"),
                                             ("resets 3pm (America/New_York)", "2026-11-02 21:00")])
def test_parse_reset_reads_the_clock_time_and_the_zone(text, expected):
    from app.headless import parse_reset
    got = parse_reset("You've hit your session limit · " + text, utc(2026, 11, 2, 5, 0), "Europe/Zurich")
    assert local(got)[:16] == expected


def test_parse_reset_without_a_time_is_none():
    from app.headless import parse_reset
    assert parse_reset("hit your session limit", utc(2026, 11, 2, 5, 0), "Europe/Zurich") is None


def test_run_step_accepts_a_command_with_options(monkeypatch):
    seen = {}

    class FakeProc:
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd) or FakeProc())
    assert scheduler.run_step(["finish", "--headless-done", "x.json"], threading.Event()) == 0
    assert seen["cmd"][1:] == ["-m", "app", "finish", "--headless-done", "x.json"]


def test_headless_config_defaults_and_validation(tmp_path):
    cfg = load_config()
    assert cfg["headless"]["enabled"] is False and cfg["headless"]["resume_attempts"] == 2
    assert cfg["headless"]["retry_latest"] == "20:00" and cfg["headless"]["retry_fallback_minutes"] == 300
    for patch in ("roles:\n    scorer:\n      model: sonnet\n", "roles:\n    scorer:\n      effort: huge\n", "max_wait_minutes: 300\n",
                  "max_wait_minutes: 100\n", "retry_latest: '8pm'\n", "resume_attempts: -1\n", "enabled: 1\n"):
        path = tmp_path / "c.yaml"
        path.write_text("headless:\n  " + patch, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)


def test_every_step_gets_the_same_run_id(tmp_path, monkeypatch):
    """v0.78.2: the scheduler makes one run id before the first step and every child process inherits it (RECRUITMENT_RUN_ID)."""
    import os
    monkeypatch.delenv("RECRUITMENT_RUN_ID", raising=False)
    cfg = headless_cfg(tmp_path)
    seen = []

    class Step(FakeDay):
        def __call__(self, name):
            seen.append(os.environ.get("RECRUITMENT_RUN_ID"))
            return super().__call__(name)

    scheduler.run_day(cfg, threading.Event(), Step(cfg), now=lambda: utc(2026, 11, 2, 5, 0), wait=answer_with(cfg))
    assert seen == ["2026-11-02T050000Z"] * 4                                       # import-descriptions, prepare, backup, finish
    assert "RECRUITMENT_RUN_ID" not in os.environ                                    # the parent's environment is left as it was
    plain = headless_cfg(tmp_path, enabled=False)
    seen.clear()
    scheduler.run_day(plain, threading.Event(), Step(plain), now=lambda: utc(2026, 11, 3, 5, 0))
    assert seen == ["2026-11-03T050000Z"] * 2                                       # prepare, backup (headless off)


def test_a_stop_while_waiting_writes_no_cancel_marker_and_no_done_file(tmp_path):
    cfg = headless_cfg(tmp_path, max_wait_minutes=275)
    stop = threading.Event()
    result = scheduler.wait_for_done(cfg, "run1", stop, lambda: utc(2026, 11, 2, 4, 0), lambda seconds: stop.set())
    assert not (llm_dir(cfg) / "cancel" / "run1").exists()
    assert not (llm_dir(cfg) / "done" / "run1.json").exists()
    assert result["status"] != "timeout"


# --- the watchdog and the guards (v0.80.6) -------------------------------------------------------------------------

def test_loop_calls_the_watchdog_at_the_start_and_before_each_day(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls = Clock(utc(2026, 11, 2, 4, 0)), []
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait,
                   max_ticks=2 * 24 * 180, watchdog=lambda: calls.append("watchdog") or 0)
    assert calls[:4] == ["watchdog", "watchdog", "prepare", "backup"]        # at the start, then before the first day
    assert calls.count("watchdog") == 3 and calls.count("prepare") == 2          # a normal day is not followed by one


def test_a_cut_off_step_is_followed_by_the_watchdog(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls = Clock(utc(2026, 11, 2, 5, 29)), []
    codes = {"prepare": -9, "backup": 0}
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or codes[n], wait=clock.wait,
                   max_ticks=10, watchdog=lambda: calls.append("watchdog") or 0)
    assert calls == ["watchdog", "watchdog", "prepare", "backup", "watchdog"]


def test_no_watchdog_after_the_loop_was_told_to_stop(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls, stop = Clock(utc(2026, 11, 2, 5, 29)), [], threading.Event()

    def step(name):
        calls.append(name)
        stop.set()
        return -1
    scheduler.loop(cfg, stop, now=clock.now, step=step, wait=clock.wait, max_ticks=10, watchdog=lambda: calls.append("watchdog") or 0)
    assert calls == ["watchdog", "watchdog", "prepare"]


def test_a_failing_heartbeat_does_not_end_the_loop(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, enabled=False)
    clock, seen = Clock(utc(2026, 11, 2, 5, 0)), []

    def broken(c):
        seen.append(1)
        raise OSError("share locked")
    monkeypatch.setattr(scheduler, "touch_heartbeat", broken)
    assert scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: 0, wait=clock.wait, max_ticks=5) == 0
    assert len(seen) == 5


def test_a_day_that_fails_with_an_oserror_does_not_end_the_loop(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls = Clock(utc(2026, 11, 2, 5, 29)), []

    def step(name):
        calls.append(name)
        raise OSError("disk full")
    assert scheduler.loop(cfg, threading.Event(), now=clock.now, step=step, wait=clock.wait, max_ticks=2 * 24 * 180) == 0
    assert calls.count("prepare") == 2                                         # the next day was computed and run


def test_command_hands_the_cleanup_watchdog_to_the_loop(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, enabled=True)
    seen = {}
    monkeypatch.setattr(scheduler, "loop", lambda c, stop, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)            # no real handler may leak into the test session
    ran = []
    monkeypatch.setattr(scheduler, "run_step", lambda name, stop, config_path=None, cfg=None: ran.append(name) or 0)
    assert scheduler.command(cfg) == 0
    seen["watchdog"]()
    assert ran == [["watchdog", "--cleanup"]]


def test_a_watchdog_that_raises_does_not_end_the_loop(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls = Clock(utc(2026, 11, 2, 5, 29)), []

    def broken():
        calls.append("watchdog")
        raise OSError("cannot start")
    assert scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait,
                          max_ticks=10, watchdog=broken) == 0
    assert calls == ["watchdog", "watchdog", "prepare", "backup"]


def test_retry_moment_reads_a_usage_limit_of_a_resumed_run(tmp_path):
    cfg = headless_cfg(tmp_path)
    done = {"status": "ok", "resumed": [{"run_id": "r0", "status": "usage_limit", "limit_message": LIMIT}]}
    assert local(scheduler.retry_moment(cfg, done, utc(2026, 11, 2, 5, 40))) == "2026-11-02 15:01 CET"


def test_the_loop_runs_the_startup_watchdog_first_and_the_normal_one_later(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    clock, calls = Clock(utc(2026, 11, 2, 4, 0)), []
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=2 * 24 * 180,
                   watchdog=lambda: calls.append("watchdog") or 0, watchdog_startup=lambda: calls.append("startup") or 0)
    assert calls[:4] == ["startup", "watchdog", "prepare", "backup"] and calls.count("startup") == 1


# ---- "Ask the writer to fix" (v0.85.0): the step `revisions` in an idle tick -----------------------------------------

def test_an_idle_tick_runs_the_revisions_step_only_when_there_is_work(tmp_path):
    cfg = cfg_for(tmp_path, enabled=False)
    cfg["headless"]["enabled"] = True
    clock, calls, checks = Clock(utc(2026, 11, 2, 4, 0)), [], []
    work = {"on": False}

    def check(c, now, relay):
        checks.append(relay)
        return work["on"]
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=3, revision_check=check)
    assert calls == [] and checks == [True, True, True]                          # looked, found nothing, started no process
    work["on"] = True
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=2, revision_check=check)
    assert calls == [["revisions"], ["revisions"]]


def test_the_quiet_window_before_the_daily_run_relays_nothing(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    cfg["headless"]["enabled"] = True
    clock, calls, checks = Clock(utc(2026, 11, 2, 4, 45)), [], []               # 05:45 local, 45 minutes before the run (quiet_minutes 60)
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=1,
                   revision_check=lambda c, now, relay: checks.append(relay) or True)
    assert calls == [["revisions", "--no-relay"]] and checks == [False]
    clock, calls = Clock(utc(2026, 11, 2, 3, 0)), []                             # 04:00 local: outside the window
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=1,
                   revision_check=lambda c, now, relay: True)
    assert calls == [["revisions"]]


def test_nothing_is_checked_with_headless_off_and_the_daily_run_is_unchanged(tmp_path):
    cfg = cfg_for(tmp_path, enabled=True, prepare_time="06:30")
    assert cfg["headless"]["enabled"] is False
    clock, calls = Clock(utc(2026, 11, 2, 4, 0)), []

    def check(c, now, relay):
        raise AssertionError("revision_check must not run with headless.enabled false")
    scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=2 * 180, revision_check=check)
    assert calls == ["prepare", "backup"]


def test_a_failing_revision_check_never_ends_the_loop(tmp_path):
    cfg = cfg_for(tmp_path, enabled=False)
    cfg["headless"]["enabled"] = True
    clock, calls = Clock(utc(2026, 11, 2, 4, 0)), []

    def check(c, now, relay):
        raise OSError("disk")
    assert scheduler.loop(cfg, threading.Event(), now=clock.now, step=lambda n: calls.append(n) or 0, wait=clock.wait, max_ticks=3, revision_check=check) == 0
    assert calls == []
