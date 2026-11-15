"""The slim web supervisor (app/websuper.py): restart on a crash, no restart on exit 0 or 3, state file, `status` line.
No real child process, no real clock."""
import logging
import threading
import time

from app import cli, websuper
from app.config import load_config


def cfg_for(tmp_path):
    cfg = load_config()
    cfg["paths"]["logs"] = str(tmp_path / "logs")
    return cfg


def run_with(cfg, codes, stop=None, tail=()):
    stop = stop or threading.Event()
    seen, waits = [], []
    it = iter(codes)

    def child(cmd):
        seen.append(cmd)
        code = next(it)
        if code is None:
            stop.set()
            return -15
        return code
    result = websuper.supervise(cfg, stop, [], child_tail=list(tail), child=child, wait=waits.append, clock=lambda: 0.0)
    return result, seen, waits


def test_a_crashed_child_is_started_again_with_growing_backoff(tmp_path):
    cfg = cfg_for(tmp_path)
    result, seen, waits = run_with(cfg, [1, 1, 1, None])
    assert result == 0 and len(seen) == 4 and waits == [5.0, 10.0, 20.0]
    assert seen[0][-1] == "web" and seen[0][1:3] == ["-m", "app"]
    assert websuper.read_state(cfg)["state"] == "stopped" and websuper.read_state(cfg)["restarts"] == 3


def test_backoff_is_capped_at_sixty_seconds(tmp_path):
    _, _, waits = run_with(cfg_for(tmp_path), [1] * 6 + [None])
    assert waits == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


def test_a_configuration_error_or_a_clean_exit_is_not_restarted(tmp_path):
    for code in (3, 0):
        cfg = cfg_for(tmp_path)
        result, seen, waits = run_with(cfg, [code])
        assert result == code and len(seen) == 1 and waits == []
        assert websuper.read_state(cfg)["state"] == "stopped" and websuper.read_state(cfg)["last_exit"] == code


def test_the_host_and_port_flags_reach_the_child(tmp_path):
    _, seen, _ = run_with(cfg_for(tmp_path), [None], tail=["--port", "8100"])
    assert seen[0][-3:] == ["web", "--port", "8100"]


def test_the_state_file_shows_restarting_between_runs(tmp_path):
    cfg = cfg_for(tmp_path)
    states = []
    stop = threading.Event()
    codes = iter([1, None])

    def child(cmd):
        states.append(websuper.read_state(cfg)["state"])
        code = next(codes)
        if code is None:
            stop.set()
        return code or -15
    websuper.supervise(cfg, stop, [], child=child, wait=lambda d: states.append(websuper.read_state(cfg)["state"]))
    assert states == ["running", "restarting", "running"]


def test_read_state_is_none_when_it_never_ran(tmp_path):
    assert websuper.read_state(cfg_for(tmp_path)) is None


def test_the_web_command_with_supervise_calls_the_supervisor(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(websuper, "supervise", lambda cfg, stop, extra, child_tail=(), watchdog=None: called.update(extra=extra, tail=child_tail, watchdog=watchdog) or 0)
    monkeypatch.setattr(cli.signal if hasattr(cli, "signal") else __import__("signal"), "signal", lambda *a: None)
    cfg_path = tmp_path / "config.yaml"                      # own paths: without --config the real logs/ folder was written
    cfg_path.write_text(
        f"paths:\n  database: {(tmp_path / 'data' / 'jobs.db').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n"
        f"  lock: {(tmp_path / 'data' / 'run.lock').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n",
        encoding="utf-8")
    log = logging.getLogger("recruitment")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    try:
        assert cli.main(["--config", str(cfg_path), "web", "--supervise", "--port", "8100"]) == 0
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)
    assert called["tail"] == ["--port", "8100"] and callable(called["watchdog"])        # v0.80.6: the hourly H27 check is handed over
    assert (tmp_path / "logs" / "recruitment.log").exists()


# --- the hourly watchdog (v0.80.6) -----------------------------------------------------------------------------------

def test_the_watchdog_runs_again_and_again_while_the_child_runs(tmp_path):
    cfg, stop, ran = cfg_for(tmp_path), threading.Event(), []

    def child(cmd):
        deadline = time.time() + 5
        while len(ran) < 2 and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
        return -15
    websuper.supervise(cfg, stop, [], child=child, wait=lambda s: None, watchdog=lambda: ran.append(1) or 0, watchdog_every=0.01)
    assert len(ran) >= 2


def test_a_failing_watchdog_does_not_stop_the_supervisor(tmp_path):
    cfg, stop, ran = cfg_for(tmp_path), threading.Event(), []

    def boom():
        ran.append(1)
        raise RuntimeError("check failed")

    def child(cmd):
        deadline = time.time() + 5
        while len(ran) < 2 and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
        return -15
    assert websuper.supervise(cfg, stop, [], child=child, wait=lambda s: None, watchdog=boom, watchdog_every=0.01) == 0
    assert len(ran) >= 2


def test_no_watchdog_thread_without_a_watchdog(tmp_path, monkeypatch):
    def started(*a, **k):
        raise AssertionError("a watchdog thread was started")
    monkeypatch.setattr(websuper, "_watchdog_loop", started)
    result, _, _ = run_with(cfg_for(tmp_path), [0])
    assert result == 0
