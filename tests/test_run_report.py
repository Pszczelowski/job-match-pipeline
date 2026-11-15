"""run-report (v0.78.3, goal.md 5.9): one run in text or JSON for pasting into chat. Reads the database read-only (mode=ro, no
migration), never prints job or CV text, passes every line through headless.redact. Temporary databases and folders only."""
import hashlib
import json
import logging
import sqlite3

import pytest

from app import cli, db
from app.config import load_config

SECRET = "sk-ant-oat01-" + "A1b2C3d4" * 6
RUN_OK = "2026-11-06T043000Z"
RUN_BAD = "2026-11-07T043000Z"
RUN_NEW = "2026-11-08T043000Z"


@pytest.fixture(autouse=True)
def clean_logger():
    log = logging.getLogger("recruitment")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    yield
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


@pytest.fixture
def env(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"paths:\n  database: {(tmp_path / 'data' / 'jobs.db').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n"
        f"  lock: {(tmp_path / 'data' / 'run.lock').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
        f"backup:\n  dir: {(tmp_path / 'backups').as_posix()}\n  keep: 2\n", encoding="utf-8")

    class Env:
        pass

    e = Env()
    e.cfg_path, e.cfg, e.tmp = cfg_path, load_config(cfg_path), tmp_path
    e.live = tmp_path / "data" / "jobs.db"
    e.done_dir = tmp_path / "exchange" / "llm" / "done"
    e.done_dir.mkdir(parents=True)
    conn = db.connect(e.live)
    with conn:
        for run_id, status, summary in ((RUN_OK, "ok", "prepared 12 jobs"), (RUN_BAD, "failed", "boom " + SECRET), (RUN_NEW, "ok", None)):
            conn.execute("INSERT INTO runs (id, started_at, finished_at, status, summary) VALUES (?, ?, ?, ?, ?)",
                         (run_id, run_id[:10] + "T04:30:00Z", run_id[:10] + "T04:45:00Z", status, summary))
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error, warning) VALUES (?, 'ubs_alert', 5, 2, NULL, 'only 5 found')", (RUN_BAD,))
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error, warning) VALUES (?, 'lgt', 0, 0, 'HTTP 500', NULL)", (RUN_BAD,))
        conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES (?, ?, 'batch.json', 'x', 3, 3)",
                     (RUN_BAD, "2026-11-07T04:40:00Z"))
        conn.execute("INSERT INTO imports (batch_id, file_name, sha256, status, reason, summary, imported_at) VALUES (?, 'results_x.json', 'h', 'imported', NULL, ?, ?)",
                     (RUN_BAD, json.dumps({"scored": 2}), "2026-11-07T09:00:00Z"))
        conn.execute("INSERT INTO reports (batch_id, status, attempts, error, subject, created_at, sent_at) VALUES (?, 'sent', 1, NULL, 'Top 10 Secret Bank Role', ?, ?)",
                     (RUN_BAD, "2026-11-07T09:01:00Z", "2026-11-07T09:01:05Z"))
        conn.execute("INSERT INTO alerts (key, first_seen, last_seen, message, count) VALUES ('headless_failed', ?, ?, ?, 1)",
                     ("2026-11-07T08:00:00Z", "2026-11-07T08:00:00Z", "worker failed for " + RUN_BAD + " token=" + SECRET))
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) VALUES (1, ?, '2026-11-07', 'T1', 'src', 'workday', 'no_match')",
                     ("2026-11-07T04:35:00Z",))
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) VALUES (2, ?, '2026-11-07', 'T1', 'src', 'workday', 'success')",
                     ("2026-11-07T04:36:00Z",))
    conn.close()
    (e.done_dir / f"{RUN_BAD}.json").write_text(json.dumps({
        "run_id": RUN_BAD, "status": "partial", "message": "stopped; CLAUDE_CODE_OAUTH_TOKEN=" + SECRET,
        "started": "2026-11-07T05:00:00Z", "ended": "2026-11-07T08:30:00Z",
        "roles": {"scorer": {"status": "ok", "duration_s": 100.0, "turns": 4, "stderr_tail": "Senior Quant at Secret Bank"},
                  "letters": {"status": "timeout", "duration_s": 1800.0, "turns": 30}},
        "review_summary": "The cover letter for Secret Bank role is weak",
        "attention": [], "limit_message": "", "resumed": [],
        "dropped": [{"job_id": 733, "stage": "letters", "path": "work/733_cover_letter.json"}], "dropped_more": 2}), encoding="utf-8")
    return e


def run(env, capsys, *args):
    code = cli.main(["--config", str(env.cfg_path), "run-report", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_report_is_read_only_and_redacted(env, capsys):
    before = digest(env.live)
    with sqlite3.connect(env.live) as c:
        version_before = c.execute("PRAGMA user_version").fetchone()[0]
    code, out, _ = run(env, capsys, RUN_BAD)
    assert code == 0
    assert digest(env.live) == before                                   # the database file is unchanged
    with sqlite3.connect(env.live) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == version_before
    assert SECRET not in out and "A1b2C3d4" not in out and "[redacted]" in out
    assert "Secret Bank" not in out                                     # no job text: report subject, review summary, stderr tail
    assert RUN_BAD in out and "partial" in out and "ubs_alert" in out and "HTTP 500" in out
    assert "733" in out and "letters" in out and "2 more" in out        # the drops
    assert "results_x.json" in out and "headless_failed" in out
    assert "no_match 1" in out and "success 1" in out                   # resolutions of the day
    assert "2026-11-07T05:00:00Z" in out                                # the UTC timeline


def test_run_report_does_not_migrate_an_older_database(env, capsys):
    with sqlite3.connect(env.live) as c:
        c.execute("PRAGMA user_version = 3")
    code, out, err = run(env, capsys, RUN_BAD)
    with sqlite3.connect(env.live) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
    assert code in (0, 1)                                               # tolerated or refused, never migrated


def test_run_report_last_is_the_newest_run(env, capsys):
    code, out, _ = run(env, capsys, "--last")
    assert code == 0 and RUN_NEW in out and RUN_BAD not in out


def test_run_report_last_failed_skips_the_newer_good_run(env, capsys):
    code, out, _ = run(env, capsys, "--last-failed")
    assert code == 0 and RUN_BAD in out and RUN_NEW not in out


def test_run_report_last_failed_when_none_failed(env, capsys):
    with sqlite3.connect(env.live) as c:
        c.execute("UPDATE runs SET status = 'ok'")
    (env.done_dir / f"{RUN_BAD}.json").unlink()
    code, out, err = run(env, capsys, "--last-failed")
    assert code == 1 and "no failed run" in err


def test_run_report_unknown_id(env, capsys):
    code, out, err = run(env, capsys, "2020-01-01T000000Z")
    assert code == 1 and "no run" in err and out == ""


def test_run_report_missing_done_file_degrades(env, capsys):
    code, out, _ = run(env, capsys, RUN_OK)
    assert code == 0 and "prepared 12 jobs" in out
    assert "done file: missing" in out


def test_run_report_unreadable_done_file_degrades(env, capsys):
    (env.done_dir / f"{RUN_OK}.json").write_text("{not json", encoding="utf-8")
    code, out, _ = run(env, capsys, RUN_OK)
    assert code == 0 and "done file: unreadable" in out


def test_run_report_json(env, capsys):
    code, out, _ = run(env, capsys, RUN_BAD, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["run_id"] == RUN_BAD and data["status"] == "failed"
    assert data["done"]["status"] == "partial" and data["done"]["dropped"][0]["job_id"] == 733
    assert data["done"]["dropped_more"] == 2
    assert {a["adapter"] for a in data["adapters"]} == {"ubs_alert", "lgt"}
    assert data["resolutions"] == {"no_match": 1, "success": 1}
    assert SECRET not in out and "Secret Bank" not in out


def test_run_report_no_database(env, capsys):
    env.live.unlink()
    code, out, err = run(env, capsys, "--last")
    assert code == 1 and "database" in err


def test_run_report_needs_one_selector(env, capsys):
    code, out, err = run(env, capsys)
    assert code == 1 and "exactly one" in err
    code, out, err = run(env, capsys, RUN_OK, "--last")
    assert code == 1 and "exactly one" in err
