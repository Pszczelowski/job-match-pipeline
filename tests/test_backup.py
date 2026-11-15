"""Database backup, verification and restore (goal.md 5.9, 5.3.3). Temporary folders only."""
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import backup, cli, db, pipeline
from app.config import ConfigError, load_config

T0 = datetime(2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc)
TITLE = "Secret Analyst Title"


@pytest.fixture(autouse=True)
def clean_logger(monkeypatch):
    monkeypatch.setattr(cli, "load_env", lambda *a, **k: {})
    log = logging.getLogger("recruitment")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    yield
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"paths:\n  database: {(tmp_path / 'data' / 'jobs.db').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n"
        f"  lock: {(tmp_path / 'data' / 'run.lock').as_posix()}\n"
        f"backup:\n  dir: {(tmp_path / 'backups').as_posix()}\n  keep: 3\n", encoding="utf-8")
    cfg = load_config(cfg_path)

    class Env:
        pass

    e = Env()
    e.cfg_path, e.cfg = cfg_path, cfg
    e.live = tmp_path / "data" / "jobs.db"
    e.dir = tmp_path / "backups"
    e.tmp = tmp_path
    return e


def run(env, *args):
    return cli.main(["--config", str(env.cfg_path), *args])


def add_job(conn, key, title=TITLE):
    cur = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                       (key, title, "Acme", "new", db.utcnow(), db.utcnow()))
    conn.commit()
    return cur.lastrowid


def populate(env):
    """Two open jobs and one archived job."""
    conn = db.connect(env.live)
    add_job(conn, "a|1")
    add_job(conn, "a|2")
    db.archive_job(conn, add_job(conn, "a|3"), "filter", "language_c1_required", "needs C1")
    conn.close()


def counts(path):
    c = sqlite3.connect(path)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("jobs", "job_archive")}
    finally:
        c.close()


def backups_in(env):
    return sorted(p.name for p in env.dir.iterdir()) if env.dir.exists() else []


# --- backup ------------------------------------------------------------------------------------------------------

def test_backup_creates_a_valid_copy(env, capsys):
    populate(env)
    assert run(env, "backup") == 0
    out = capsys.readouterr().out
    files = backups_in(env)
    assert len(files) == 1 and re.fullmatch(r"jobs_\d{8}T\d{6}Z\.db", files[0])        # no .tmp left over
    copy = env.dir / files[0]
    raw = sqlite3.connect(copy)
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert raw.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    raw.close()
    assert counts(copy) == counts(env.live) == {"jobs": 2, "job_archive": 1}
    assert files[0] in out and TITLE not in out                                          # path printed, content never


def test_backup_without_a_database_fails_and_writes_nothing(env, capsys):
    assert run(env, "backup") == 1
    assert "not found" in capsys.readouterr().err
    assert backups_in(env) == []


def test_backup_works_while_another_connection_holds_the_database_in_wal_mode(env):
    populate(env)
    reader = db.connect(env.live)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM jobs").fetchone()                  # an open read transaction
    writer = db.connect(env.live)
    add_job(writer, "a|late")                                               # committed into the WAL, not yet in the main file
    assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    reader.close()
    writer.close()
    assert info.counts["jobs"] == 3 and counts(info.path)["jobs"] == 3      # the WAL content is in the copy
    assert sqlite3.connect(info.path).execute("PRAGMA journal_mode").fetchone()[0] == "delete"   # one single file
    assert backups_in(env) == [info.path.name]


def test_a_failing_check_deletes_the_copy_and_exits_non_zero(env, monkeypatch, capsys):
    populate(env)

    def bad(path):
        raise backup.BackupError("integrity check failed for x: broken")

    monkeypatch.setattr(backup, "_inspect", bad)
    assert run(env, "backup") == 1
    assert "integrity check failed" in capsys.readouterr().err
    assert backups_in(env) == []                                            # neither the copy nor a temp file


def test_an_os_error_in_the_backup_is_reported_as_a_failed_backup(env, monkeypatch, capsys):
    populate(env)
    reported = []
    monkeypatch.setattr(cli.alerts, "report_backup", lambda cfg, error, **kw: reported.append(error))

    def cannot_delete(directory, keep):
        raise PermissionError("old copy is read-only")

    monkeypatch.setattr(backup, "rotate", cannot_delete)
    assert run(env, "backup") == 1
    assert "read-only" in capsys.readouterr().err and len(reported) == 1 and "read-only" in reported[0]


def test_a_backup_folder_that_cannot_be_created_is_reported_as_a_failed_backup(env, monkeypatch, capsys):
    populate(env)
    reported = []
    monkeypatch.setattr(cli.alerts, "report_backup", lambda cfg, error, **kw: reported.append(error))
    env.dir.write_text("not a folder", encoding="utf-8")                      # the configured backup.dir is a regular file
    assert run(env, "backup") == 1
    assert capsys.readouterr().err.startswith("ERROR") and len(reported) == 1 and reported[0]


def test_row_count_difference_between_source_and_copy_is_an_error(env, monkeypatch):
    populate(env)
    real = backup._inspect

    def short(path):
        info = real(path)
        info.counts["jobs"] -= 1
        return info

    monkeypatch.setattr(backup, "_inspect", short)
    with pytest.raises(backup.BackupError, match="row counts differ in: jobs"):
        backup.create_backup(env.live, env.dir, 14, now=T0)
    assert backups_in(env) == []


def test_schema_version_difference_between_source_and_copy_is_an_error(env, monkeypatch):
    populate(env)
    real = backup._inspect

    def other(path):
        info = real(path)
        info.version += 1
        return info

    monkeypatch.setattr(backup, "_inspect", other)
    with pytest.raises(backup.BackupError, match="schema version differs"):
        backup.create_backup(env.live, env.dir, 14, now=T0)
    assert backups_in(env) == []


def test_two_backups_in_the_same_second_do_not_overwrite(env):
    populate(env)
    backup.create_backup(env.live, env.dir, 14, now=T0)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.create_backup(env.live, env.dir, 14, now=T0)
    assert len(backups_in(env)) == 1


# --- rotation ----------------------------------------------------------------------------------------------------

def test_rotation_keeps_the_newest_and_ignores_other_files(env):
    populate(env)
    env.dir.mkdir()
    others = ["jobs.db.before-migration", "notes.txt", "jobs_pre-restore_20200101T000000Z.db", "jobs_2026.db",
              "jobs_20200101T000000Z.db.tmp", "xjobs_20200101T000000Z.db"]
    for name in others:
        (env.dir / name).write_bytes(b"mine")
    for i in range(5):
        backup.create_backup(env.live, env.dir, 3, now=T0 + timedelta(days=i))
    names = backups_in(env)
    stamps = [n for n in names if re.fullmatch(r"jobs_\d{8}T\d{6}Z\.db", n)]
    assert stamps == ["jobs_20261103T030000Z.db", "jobs_20261104T030000Z.db", "jobs_20261105T030000Z.db"]   # newest 3
    assert all((env.dir / n).read_bytes() == b"mine" for n in others)                                       # untouched


def test_the_backup_command_rotates_to_the_configured_keep(env):
    populate(env)
    env.dir.mkdir()
    for i in range(4):                                                       # four old copies, keep is 3
        (env.dir / f"jobs_2026010{i + 1}T000000Z.db").write_bytes(b"old")
    assert run(env, "backup") == 0
    names = backups_in(env)
    assert len(names) == 3 and names[0] == "jobs_20260103T000000Z.db"        # the two oldest went, the new one stays


# --- verify ------------------------------------------------------------------------------------------------------

def test_verify_accepts_a_good_backup(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    assert run(env, "verify-backup", str(info.path)) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK") and f"schema version {len(db.MIGRATIONS)}" in out and TITLE not in out


@pytest.mark.parametrize("damage", ["header", "truncate"])
def test_verify_detects_a_corrupted_copy(env, capsys, damage):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    data = info.path.read_bytes()
    if damage == "header":
        info.path.write_bytes(b"X" * 200 + data[200:])
    else:
        info.path.write_bytes(data[: len(data) // 2])
    capsys.readouterr()
    assert run(env, "verify-backup", str(info.path)) == 1
    assert "ERROR" in capsys.readouterr().err


def test_verify_reports_a_missing_file_and_a_foreign_database(env, capsys):
    assert run(env, "verify-backup", str(env.tmp / "nothing.db")) == 1
    foreign = env.tmp / "foreign.db"
    c = sqlite3.connect(foreign)
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    assert run(env, "verify-backup", str(foreign)) == 1
    assert "not a job-match database" in capsys.readouterr().err


def test_verify_does_not_change_the_file(env):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    before = info.path.read_bytes()
    backup.verify_backup(info.path)
    assert info.path.read_bytes() == before and backups_in(env) == [info.path.name]


# --- restore -----------------------------------------------------------------------------------------------------

def test_restore_requires_yes(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    conn = db.connect(env.live)
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    assert run(env, "restore-backup", str(info.path)) == 1
    assert "--yes" in capsys.readouterr().err
    assert counts(env.live)["jobs"] == 0                                    # nothing was replaced
    assert backups_in(env) == [info.path.name]                              # and no safety copy was made


def test_round_trip_archive_survives_backup_and_restore(env, capsys):
    """goal.md 5.3.3: the archive survives backup and restore (through the commands)."""
    populate(env)
    assert run(env, "backup") == 0
    saved = env.dir / backups_in(env)[0]
    conn = db.connect(env.live)                                              # change the data after the backup
    conn.execute("DELETE FROM job_archive")
    conn.execute("DELETE FROM jobs")
    add_job(conn, "b|new", title="Added later")
    conn.close()
    assert counts(env.live) == {"jobs": 1, "job_archive": 0}
    capsys.readouterr()

    assert run(env, "restore-backup", str(saved), "--yes") == 0
    conn = db.connect(env.live)
    assert counts(env.live) == {"jobs": 2, "job_archive": 1}
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE title = 'Added later'").fetchone()[0] == 0
    row = conn.execute("SELECT reason_code, comment, title, rejection_stage FROM job_archive").fetchone()
    assert tuple(row) == ("language_c1_required", "needs C1", TITLE, "filter")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.restore_job(conn, row_id(conn)) > 0                            # the restored archive is usable
    conn.close()
    assert TITLE not in capsys.readouterr().out


def row_id(conn):
    return conn.execute("SELECT id FROM job_archive").fetchone()[0]


def test_restore_makes_a_safety_copy_of_the_current_database_first(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    conn = db.connect(env.live)                                              # current state differs from the backup
    add_job(conn, "c|current")
    conn.close()
    assert counts(env.live)["jobs"] == 3
    assert run(env, "restore-backup", str(info.path), "--yes") == 0
    safety = [p for p in env.dir.iterdir() if p.name.startswith("jobs_pre-restore_")]
    assert len(safety) == 1 and safety[0].name.endswith("Z.db")
    assert counts(safety[0])["jobs"] == 3                                    # the state before the restore
    assert counts(env.live)["jobs"] == 2
    assert str(safety[0]) in capsys.readouterr().out


def test_safety_copy_is_not_rotated_away_by_later_backups(env):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 3, now=T0)
    backup.restore_backup(info.path, env.live, env.dir, now=T0 + timedelta(hours=1))
    for i in range(2, 8):
        backup.create_backup(env.live, env.dir, 3, now=T0 + timedelta(days=i))
    names = backups_in(env)
    assert any(n.startswith("jobs_pre-restore_") for n in names)
    assert len([n for n in names if not n.startswith("jobs_pre-restore_")]) == 3


def test_restore_is_refused_while_a_run_holds_the_lock(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    conn = db.connect(env.live)
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    with pipeline.run_lock(env.cfg):
        assert run(env, "restore-backup", str(info.path), "--yes") == 2
    assert "another run is in progress" in capsys.readouterr().err
    assert counts(env.live)["jobs"] == 0 and backups_in(env) == [info.path.name]


def test_restore_of_a_newer_schema_is_refused(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    raw = sqlite3.connect(info.path)
    raw.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    raw.close()
    before = env.live.read_bytes()
    assert run(env, "restore-backup", str(info.path), "--yes") == 1
    assert "newer than this code" in capsys.readouterr().err
    assert counts(env.live) == {"jobs": 2, "job_archive": 1}
    assert backups_in(env) == [info.path.name]                              # refused before any safety copy
    assert env.live.read_bytes() == before


def test_restore_refuses_a_corrupted_backup_and_keeps_the_database(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    info.path.write_bytes(info.path.read_bytes()[:5000])
    assert run(env, "restore-backup", str(info.path), "--yes") == 1
    assert counts(env.live) == {"jobs": 2, "job_archive": 1}
    assert backups_in(env) == [info.path.name]


def test_restore_of_an_older_schema_works_and_the_migrations_apply_on_next_open(env, capsys):
    old = env.tmp / "old.db"
    raw = sqlite3.connect(old)
    for number in range(1, 6):
        raw.executescript(f"BEGIN;\n{db.MIGRATIONS[number - 1]}\nPRAGMA user_version = {number};\nCOMMIT;")
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('o|1','Old','Acme','new','t','t')")
    raw.commit()
    raw.close()
    populate(env)
    assert run(env, "restore-backup", str(old), "--yes") == 0
    assert "older than this code" in capsys.readouterr().out
    check = sqlite3.connect(env.live)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 5
    check.close()
    conn = db.connect(env.live)                                              # the normal open migrates
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert conn.execute("SELECT title FROM jobs").fetchone()[0] == "Old"
    conn.close()


def test_restore_without_a_current_database_makes_no_safety_copy(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    for suffix in ("", "-wal", "-shm"):
        (env.live.parent / f"jobs.db{suffix}").unlink(missing_ok=True)
    assert run(env, "restore-backup", str(info.path), "--yes") == 0
    assert "no previous database" in capsys.readouterr().out
    assert counts(env.live) == {"jobs": 2, "job_archive": 1} and backups_in(env) == [info.path.name]


def test_restore_over_a_damaged_database_keeps_its_raw_file_and_succeeds(env, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    for suffix in ("-wal", "-shm"):
        (env.live.parent / f"jobs.db{suffix}").unlink(missing_ok=True)
    env.live.write_bytes(b"garbage" * 1000)
    assert run(env, "restore-backup", str(info.path), "--yes") == 0
    kept = [p for p in env.dir.iterdir() if p.name.endswith(".unverified.db")]
    assert len(kept) == 1 and kept[0].read_bytes() == b"garbage" * 1000
    assert counts(env.live) == {"jobs": 2, "job_archive": 1}


# --- config ------------------------------------------------------------------------------------------------------

def test_backup_defaults():
    cfg = load_config()
    assert cfg["backup"] == {"dir": "backups", "keep": 14}


@pytest.mark.parametrize("text, message", [
    ("backup:\n  keep: 0\n", "backup.keep"),
    ("backup:\n  keep: 366\n", "backup.keep"),
    ("backup:\n  keep: 2.5\n", "backup.keep"),
    ("backup:\n  keep: true\n", "backup.keep"),
    ("backup:\n  keep: many\n", "backup.keep"),
    ("backup:\n  dir: ''\n", "backup.dir"),
    ("backup:\n  dir: 5\n", "backup.dir"),
    ("backup:\n  folder: x\n", "unknown config key"),
])
def test_invalid_backup_config_is_rejected(tmp_path, text, message):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(p)


@pytest.mark.parametrize("keep", [1, 365])
def test_backup_keep_limits_are_accepted(tmp_path, keep):
    p = tmp_path / "config.yaml"
    p.write_text(f"backup:\n  keep: {keep}\n", encoding="utf-8")
    assert load_config(p)["backup"]["keep"] == keep


# --- checks that only the deep verification can catch --------------------------------------------------------------

def test_verify_detects_damage_that_leaves_the_file_readable(env, capsys):
    """An index entry altered inside the file: SQLite still opens it, only `PRAGMA integrity_check` notices."""
    populate(env)
    conn = db.connect(env.live)
    add_job(conn, "zzzzzzzz-unique-dedup-key")
    conn.close()
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    data = bytearray(info.path.read_bytes())
    needle = b"zzzzzzzz-unique-dedup-key"
    assert data.count(needle) >= 2                                           # row page and index page
    at = bytes(data).rindex(needle)
    data[at:at + len(needle)] = b"yyyyyyyy-unique-dedup-key"
    info.path.write_bytes(bytes(data))
    raw = sqlite3.connect(info.path)
    raw.execute("SELECT COUNT(*) FROM jobs").fetchone()                      # opens and reads fine
    raw.close()
    capsys.readouterr()
    assert run(env, "verify-backup", str(info.path)) == 1
    assert "integrity check failed" in capsys.readouterr().err


def test_restore_checks_the_result_and_names_the_safety_copy_on_a_mismatch(env, monkeypatch):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    real = backup._inspect

    def wrong_after_restore(path):
        r = real(path)
        if path == env.live:
            r.counts["jobs"] += 1
        return r

    monkeypatch.setattr(backup, "_inspect", wrong_after_restore)
    with pytest.raises(backup.BackupError, match=r"does not match the backup; the previous database is kept in .*jobs_pre-restore_"):
        backup.restore_backup(info.path, env.live, env.dir, now=T0 + timedelta(hours=1))


def test_an_interrupted_restore_exits_143_and_frees_the_lock(env, monkeypatch):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)

    def stopped(*a, **k):
        raise pipeline.Interrupted()
    monkeypatch.setattr(backup, "restore_backup", stopped)
    assert run(env, "restore-backup", str(info.path), "--yes") == 143
    assert not pipeline.lock_is_held(env.cfg)


def test_a_stop_after_the_restore_completed_says_so(env, monkeypatch, capsys):
    populate(env)
    info, _ = backup.create_backup(env.live, env.dir, 14, now=T0)
    real = pipeline.run_lock

    from contextlib import contextmanager

    @contextmanager
    def late_stop(cfg):
        with real(cfg):
            yield
        raise pipeline.Interrupted()
    monkeypatch.setattr(pipeline, "run_lock", late_stop)
    assert run(env, "restore-backup", str(info.path), "--yes") == 143
    assert "restore completed, then interrupted" in capsys.readouterr().err
