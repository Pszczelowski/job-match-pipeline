import logging
import re
from datetime import datetime, timedelta, timezone

from app import cli
from app.config import load_config


def test_every_log_line_has_utc_time_and_run_id(tmp_path, monkeypatch):
    """v0.78.2: `2026-11-07T06:04:03Z INFO run=<id> ...`, a stamp in UTC, the run id of the environment (`-` for the web app),
    and a token-like string never reaches the file."""
    cfg = load_config()
    cfg["paths"]["logs"] = str(tmp_path / "logs")
    log = logging.getLogger("recruitment")
    saved = list(log.handlers)
    for handler in saved:
        log.removeHandler(handler)
    try:
        monkeypatch.setenv("RECRUITMENT_RUN_ID", "2026-11-07T060403Z")
        cli.setup_logging(cfg)
        log.info("run 2026-11-07T060403Z: prepare started")
        monkeypatch.delenv("RECRUITMENT_RUN_ID")
        log.warning("a web line job=7 key sk-ant-made-up-secret-value")
        for handler in log.handlers:
            handler.flush()
    finally:
        for handler in list(log.handlers):
            handler.close()
            log.removeHandler(handler)
        for handler in saved:
            log.addHandler(handler)
    text = (tmp_path / "logs" / "recruitment.log").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 2
    pattern = r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (INFO|WARNING) run=(\S+) "
    first, second = (re.match(pattern, line) for line in lines)
    assert first and second and first.group(3) == "2026-11-07T060403Z" and second.group(3) == "-"
    stamp = datetime.strptime(first.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=2)             # the stamp is UTC, not local time
    assert "job=7" in lines[1] and "sk-ant-made-up-secret-value" not in text
