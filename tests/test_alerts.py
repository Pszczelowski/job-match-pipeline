"""Health alerts by e-mail (goal.md 5.9, v0.36.0). Fake SMTP and IMAP only; nothing touches the network or the real config."""
import json
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, backup, cli, db
from app.config import ConfigError, load_config
from app.descriptions_fetch import FetchError
from app.mailbox import MailAuthError, MailError
from app.pipeline import prepare, status
from tests.test_adapters import LIST_URL, Pages
from tests.test_cli import clean_logger, config_file, providers_factory, run  # noqa: F401  (fixtures)
from tests.test_mailbox import FakeIMAP, four_alerts, make_mail
from tests.test_pipeline import providers, run_prepare, write_results  # noqa: F401
from tests.test_report import PASSWORD, FakeSMTP

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)          # 07:00 in Zurich
NEXT_DAY = NOW + timedelta(days=1)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"),
                      database=str(tmp_path / "t.db"))
    c["email"]["retry_wait_seconds"] = 0
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.failures = [], [], []


def deliver(conn, cfg, now=NOW):
    return alerts.deliver(conn, cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=now)


def subjects():
    return [m["Subject"] for m in FakeSMTP.sent]


def row(conn, key):
    return conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()


def open_keys(conn):
    return [a["key"] for a in alerts.open_alerts(conn)]


# --- migration 15 ---------------------------------------------------------------------------------

def test_migration_15_adds_the_table_and_keeps_the_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = db.sqlite3.connect(str(path))
    for number, script in enumerate(db.MIGRATIONS[:14], start=1):
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('k', 'Analyst', 'SIX', 'scored', 'a', 'b')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS) >= 15
    assert c.execute("SELECT title FROM jobs").fetchone()[0] == "Analyst"
    assert [r[1] for r in c.execute("PRAGMA table_info(alerts)")] == [
        "id", "key", "first_seen", "last_seen", "last_sent", "resolved_at", "resolved_sent_at", "message", "count"]
    alerts.observe(c, load_config(), "backup_failed", "x", NOW)
    with pytest.raises(db.sqlite3.IntegrityError):                     # the key is unique
        c.execute("INSERT INTO alerts (key, first_seen, last_seen, message) VALUES ('backup_failed', 'a', 'b', 'c')")
    c.close()


def test_the_migration_count_is_24():                                  # was 23: migration 24 (v0.87.1)
    assert len(db.MIGRATIONS) == 24


# --- the rules: raise once a day, resolve once ----------------------------------------------------

def test_an_alert_is_mailed_once_and_not_again_the_same_day(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "the backup failed: disk full", NOW)
    r = deliver(conn, cfg)
    assert r.sent == ["backup_failed"] and subjects() == ["Recruitment alert: backup failed"]
    msg = FakeSMTP.sent[0]
    assert msg["From"] == msg["To"] == cfg["email"]["sender"] and msg["X-Recruitment-Alert"] == "backup_failed"
    assert "disk full" in msg.get_content() and "First seen: 2 Nov 2026 07:00 (Europe/Zurich)" in msg.get_content()
    assert not msg.is_multipart()                                      # plain text
    alerts.observe(conn, cfg, "backup_failed", "the backup failed: disk full", NOW + timedelta(hours=3))
    assert deliver(conn, cfg, NOW + timedelta(hours=3)).sent == [] and len(FakeSMTP.sent) == 1
    assert row(conn, "backup_failed")["count"] == 2


def test_it_is_mailed_again_on_the_next_day_while_the_condition_lasts(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    deliver(conn, cfg)
    alerts.observe(conn, cfg, "backup_failed", "x", NEXT_DAY)
    assert deliver(conn, cfg, NEXT_DAY).sent == ["backup_failed"] and len(FakeSMTP.sent) == 2
    assert "Seen 2 time(s)" in FakeSMTP.sent[1].get_content()


def test_a_day_is_a_calendar_day_in_the_configured_zone(conn, cfg):
    late = datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc)          # 23:30 on 2 Nov in Zurich
    alerts.observe(conn, cfg, "backup_failed", "x", late)
    deliver(conn, cfg, late)
    assert deliver(conn, cfg, late + timedelta(minutes=29)).sent == []       # 23:59 Zurich, same day
    assert deliver(conn, cfg, late + timedelta(minutes=31)).sent == ["backup_failed"]    # 00:01 on 3 Nov: a new day, 31 minutes later


def test_a_cleared_condition_gives_one_resolved_mail(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    deliver(conn, cfg)
    alerts.clear(conn, "backup_failed", NOW + timedelta(hours=2))
    r = deliver(conn, cfg, NOW + timedelta(hours=2))
    assert r.resolved == ["backup_failed"] and subjects()[-1] == "Recruitment alert resolved: backup failed"
    assert "resolved" in FakeSMTP.sent[-1].get_content() and open_keys(conn) == []
    assert deliver(conn, cfg, NEXT_DAY).resolved == [] and len(FakeSMTP.sent) == 2         # never twice
    alerts.clear(conn, "backup_failed", NEXT_DAY)                                          # clearing again changes nothing
    assert row(conn, "backup_failed")["resolved_at"] == "2026-11-02T08:00:00Z"


def test_an_alert_that_was_never_mailed_is_closed_silently(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    alerts.clear(conn, "backup_failed", NOW)
    assert deliver(conn, cfg).sent == [] and FakeSMTP.sent == []


def test_a_condition_that_comes_back_is_a_new_incident_but_keeps_the_daily_limit(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "first", NOW)
    deliver(conn, cfg)
    alerts.clear(conn, "backup_failed", NOW + timedelta(hours=1))
    deliver(conn, cfg, NOW + timedelta(hours=1))
    later = NOW + timedelta(hours=2)
    alerts.observe(conn, cfg, "backup_failed", "second", later)
    a = row(conn, "backup_failed")
    assert (a["count"], a["first_seen"], a["resolved_at"], a["resolved_sent_at"], a["message"]) == (1, "2026-11-02T08:00:00Z", None, None, "second")
    assert deliver(conn, cfg, later).sent == [] and len(FakeSMTP.sent) == 2         # same day: no third mail
    assert deliver(conn, cfg, NEXT_DAY).sent == ["backup_failed"]                    # next day: told again
    alerts.clear(conn, "backup_failed", NEXT_DAY)
    assert deliver(conn, cfg, NEXT_DAY).resolved == ["backup_failed"]


def test_a_resolved_mail_is_not_sent_for_an_incident_the_user_was_never_told_about(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "first", NOW)
    deliver(conn, cfg)
    alerts.clear(conn, "backup_failed", NOW)
    deliver(conn, cfg)
    alerts.observe(conn, cfg, "backup_failed", "second", NOW + timedelta(minutes=30))   # same day: suppressed
    alerts.clear(conn, "backup_failed", NOW + timedelta(hours=1))
    assert deliver(conn, cfg, NOW + timedelta(hours=1)).resolved == [] and len(FakeSMTP.sent) == 2


def test_every_alert_has_its_own_mail(conn, cfg):
    alerts.observe(conn, cfg, "run_failed:finish", "a", NOW)
    alerts.observe(conn, cfg, "adapter_zero:six", "b", NOW)
    alerts.observe(conn, cfg, "mail_zero:ubs_alert", "c", NOW)
    deliver(conn, cfg)
    assert sorted(subjects()) == ["Recruitment alert: SIX career site returned no jobs", "Recruitment alert: UBS alert mails contained no jobs",
                                  "Recruitment alert: finish failed"]


def test_deliver_with_keys_mails_and_resolves_only_those_alerts(conn, cfg):
    alerts.observe(conn, cfg, alerts.MISSED_SCHEDULE_KEY, "old", NOW)
    alerts.observe(conn, cfg, "run_failed:finish", "a", NOW)
    alerts.deliver(conn, cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=NOW, keys={alerts.MISSED_SCHEDULE_KEY})
    assert subjects() == ["Recruitment alert: no successful prepare for more than 30 hours"]
    assert row(conn, "run_failed:finish")["last_sent"] is None
    alerts.clear(conn, alerts.MISSED_SCHEDULE_KEY, NOW)
    alerts.clear(conn, "run_failed:finish", NOW)
    alerts.observe(conn, cfg, "run_failed:finish", "b", NOW)         # reopened: it was never mailed, so no resolved mail either way
    alerts.deliver(conn, cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=NOW, keys={alerts.MISSED_SCHEDULE_KEY})
    assert subjects()[-1] == "Recruitment alert resolved: no successful prepare for more than 30 hours" and len(subjects()) == 2


def test_the_titles_and_hints_of_the_watchdog_alerts(cfg):
    assert alerts.short_title("run_interrupted:prepare") == "prepare was interrupted"
    assert alerts.short_title("run_interrupted:finish") == "finish was interrupted"
    assert alerts.short_title(alerts.MISSED_SCHEDULE_KEY) == "no successful prepare for more than 30 hours"
    assert "DEPLOYMENT_MANUAL 7" in alerts._hint("run_interrupted:prepare")
    assert "docker compose exec recruitment python -m app prepare" in alerts._hint(alerts.MISSED_SCHEDULE_KEY)


# --- sending that fails ---------------------------------------------------------------------------

def test_a_failed_send_keeps_the_alert_pending_and_a_later_run_retries(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("down")] * 3             # all three tries of email.send_attempts
    r = deliver(conn, cfg)
    assert r.sent == [] and "SMTPServerDisconnected" in r.failed and row(conn, "backup_failed")["last_sent"] is None
    assert [e[0] for e in FakeSMTP.events].count("login") == 3
    assert deliver(conn, cfg, NOW + timedelta(minutes=5)).sent == ["backup_failed"]


def test_the_first_failure_stops_the_other_attempts(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    alerts.observe(conn, cfg, "run_failed:finish", "y", NOW)
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("down")] * 3
    deliver(conn, cfg)
    assert [e[0] for e in FakeSMTP.events].count("connect") == 3 and FakeSMTP.sent == []


def test_a_refused_smtp_login_is_not_retried_and_becomes_its_own_alert(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    FakeSMTP.failures = [smtplib.SMTPAuthenticationError(535, b"bad credentials")]
    r = deliver(conn, cfg)
    assert r.auth and [e[0] for e in FakeSMTP.events].count("login") == 1
    assert open_keys(conn) == ["backup_failed", "gmail_auth:smtp"] and row(conn, "backup_failed")["last_sent"] is None
    # the password works again: both are mailed, the SMTP alert is closed because a mail went out, and it says so
    r = deliver(conn, cfg, NOW + timedelta(hours=1))
    assert sorted(r.sent) == ["backup_failed", "gmail_auth:smtp"] and r.resolved == ["gmail_auth:smtp"]
    assert open_keys(conn) == ["backup_failed"]


def test_a_missing_app_password_is_a_login_problem_not_a_crash(conn, cfg, monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    r = deliver(conn, cfg)
    assert r.auth and "not set" in r.failed and FakeSMTP.events == []


def test_delivery_never_raises(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("unexpected")
    r = alerts.deliver(conn, cfg, smtp_factory=Boom, sleep=lambda s: None, now=NOW)
    assert r.sent == [] and r.failed == "RuntimeError" and row(conn, "backup_failed")["last_sent"] is None


def test_a_dead_database_never_raises_while_recording(cfg):
    closed = db.connect(":memory:")
    closed.close()
    alerts.observe(closed, cfg, "backup_failed", "x", NOW)
    alerts.clear(closed, "backup_failed", NOW)


# --- alerts switched off ---------------------------------------------------------------------------

def test_with_alerts_off_they_are_recorded_but_nothing_is_sent(conn, cfg):
    cfg["monitoring"]["alerts"] = False
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    r = deliver(conn, cfg)
    assert r.disabled and FakeSMTP.events == [] and FakeSMTP.sent == []
    s = status(conn, cfg, NOW)
    assert s["alerts_enabled"] is False and [a["key"] for a in s["open_alerts"]] == ["backup_failed"] and s["open_alerts"][0]["mailed"] is False
    cfg["monitoring"]["alerts"] = True                                  # switched on later: the open alert is mailed then
    assert deliver(conn, cfg, NOW + timedelta(hours=1)).sent == ["backup_failed"]


def test_the_config_value_is_validated(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("monitoring:\n  alerts: 'yes'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="monitoring.alerts"):
        load_config(p)
    p.write_text("monitoring:\n  alerts: false\n", encoding="utf-8")
    assert load_config(p)["monitoring"]["alerts"] is False


# --- status ----------------------------------------------------------------------------------------

def test_status_lists_the_open_alerts_with_first_seen_and_count(conn, cfg):
    alerts.observe(conn, cfg, "backup_failed", "x", NOW)
    alerts.observe(conn, cfg, "backup_failed", "x", NEXT_DAY)
    alerts.observe(conn, cfg, "adapter_zero:six", "y", NEXT_DAY)
    alerts.clear(conn, "adapter_zero:six", NEXT_DAY)
    s = status(conn, cfg, NOW)
    assert s["alerts_enabled"] is True
    assert [(a["key"], a["first_seen"], a["count"], a["mailed"]) for a in s["open_alerts"]] == [("backup_failed", "2026-11-02T06:00:00Z", 2, False)]
    json.dumps(s)


# --- no secret, no personal data ------------------------------------------------------------------

def test_safe_text_removes_the_password_the_address_and_long_text(cfg):
    cfg["location"]["car_origin_address"] = "Musterstrasse 12"
    text = alerts.safe_text(cfg, f"login {PASSWORD} failed\nat Musterstrasse 12 {PASSWORD}" + " x" * 400)
    assert PASSWORD not in text and "Musterstrasse" not in text and "\n" not in text and len(text) == 300 and "***" in text


def test_the_app_password_is_removed_also_when_it_was_written_with_spaces(cfg, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    assert alerts.safe_text(cfg, "got abcdefghijklmnop and abcd efgh ijkl mnop") == "got *** and ***"


def test_no_alert_text_holds_the_password_or_the_address(conn, cfg):
    cfg["location"]["car_origin_address"] = "Musterstrasse 12"
    alerts.observe(conn, cfg, "backup_failed", f"copy failed: {PASSWORD} in Musterstrasse 12", NOW)
    deliver(conn, cfg)
    alerts.clear(conn, "backup_failed", NOW)
    deliver(conn, cfg)
    stored = row(conn, "backup_failed")["message"]
    mails = "\n".join(m.as_string() for m in FakeSMTP.sent)
    assert len(FakeSMTP.sent) == 2
    for text in (stored, mails):
        assert PASSWORD not in text and "Musterstrasse" not in text
    login = [e for e in FakeSMTP.events if e[0] == "login"]
    assert login and login[0][2] == PASSWORD                                      # the password was used to log in, nowhere else


# --- the conditions ------------------------------------------------------------------------------

def add_adapter_run(conn, run_id, found, error=None, label="SIX career site"):
    db.start_run(conn, run_id)
    with conn:
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error) VALUES (?, ?, ?, 0, ?)", (run_id, label, found, error))


def test_an_adapter_with_no_jobs_in_a_row_raises_and_a_good_read_clears(conn, cfg):
    cfg["adapters"]["six"]["enabled"] = True
    n = cfg["monitoring"]["zero_jobs_alert_after"]
    for i in range(n - 1):
        add_adapter_run(conn, f"r{i}", 0)
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert open_keys(conn) == []                                                  # one short of the limit
    add_adapter_run(conn, "r-last", 0)
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert open_keys(conn) == ["adapter_zero:six"] and "SIX career site" in alerts.open_alerts(conn)[0]["message"]
    deliver(conn, cfg)
    assert subjects() == ["Recruitment alert: SIX career site returned no jobs"]
    add_adapter_run(conn, "r-good", 12)
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW + timedelta(hours=1))
    assert open_keys(conn) == []
    deliver(conn, cfg, NOW + timedelta(hours=1))
    assert subjects()[-1] == "Recruitment alert resolved: SIX career site returned no jobs"


def test_failed_reads_count_like_empty_ones_and_a_mix_does_not(conn, cfg):
    cfg["adapters"]["six"]["enabled"] = True
    add_adapter_run(conn, "r1", 0)
    add_adapter_run(conn, "r2", 0, "SIX career site: HTTP 503")
    add_adapter_run(conn, "r3", 0, "SIX career site: HTTP 503")
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert open_keys(conn) == ["adapter_zero:six"]
    add_adapter_run(conn, "r4", 7)
    add_adapter_run(conn, "r5", 0)
    add_adapter_run(conn, "r6", 0)
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert open_keys(conn) == []                                                  # the last three include a good read


def test_a_switched_off_adapter_has_no_alert(conn, cfg):
    cfg["adapters"]["six"]["enabled"] = True
    for i in range(3):
        add_adapter_run(conn, f"r{i}", 0)
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert open_keys(conn) == ["adapter_zero:six"]
    cfg["adapters"]["six"]["enabled"] = False
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert open_keys(conn) == []


def test_prepare_records_a_site_that_keeps_failing(conn, cfg, providers):
    cfg["adapters"]["six"]["enabled"] = True
    for i in range(3):
        prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP({}),
                page_fetcher=Pages(fail={LIST_URL: FetchError("HTTP 503", 503)}), page_sleep=lambda s: None,
                run_id=f"run{i}", now=NOW + timedelta(days=i))
        assert open_keys(conn) == (["adapter_zero:six"] if i == 2 else [])
    assert "HTTP 503" not in alerts.open_alerts(conn)[0]["message"]             # the site's error text is not copied into the mail


def test_repeated_empty_ubs_mails_raise_mail_zero_and_a_mail_with_jobs_clears_it(conn, cfg, providers):
    html = "These jobs matched the criteria specified for: Zurich <br><table><tr><th>Title</th><th>Job Reference #</th></tr></table>"
    messages = {str(n): make_mail(html, f"<z{n}@x>", date=f"Sat, 1{n} Nov 2026 03:00:00 +0000") for n in range(1, 4)}
    run_prepare(conn, cfg, providers, FakeIMAP(messages))
    assert open_keys(conn) == ["mail_zero:ubs_alert"] and "contained no jobs" in alerts.open_alerts(conn)[0]["message"]
    conn.execute("UPDATE mail_messages SET jobs_found = 1")                      # the saved search delivers jobs again
    conn.commit()
    run_prepare(conn, cfg, providers, FakeIMAP({}), "run2", NOW + timedelta(days=1))
    assert open_keys(conn) == []


def test_a_run_that_cannot_read_the_mailbox_leaves_mail_zero_alone(conn, cfg, providers):
    alerts.observe(conn, cfg, "mail_zero:ubs_alert", "x", NOW)

    def broken(cfg):
        raise MailError("cannot reach imap.gmail.com: TimeoutError")
    r = prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=broken, run_id="run1", now=NOW)
    assert r.mail_login is None and open_keys(conn) == ["mail_zero:ubs_alert"]


def test_a_refused_imap_login_is_gmail_auth_and_a_working_one_clears_it(conn, cfg, providers):

    def refused(cfg):
        raise MailAuthError("Gmail refused the login; check the app password and that IMAP is enabled")
    r = prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=refused, run_id="run1", now=NOW)
    assert r.mail_login is False and open_keys(conn) == ["gmail_auth:imap"]
    run_prepare(conn, cfg, providers, FakeIMAP({}), "run2", NOW + timedelta(days=1))
    assert open_keys(conn) == []


# --- the commands (exit codes, mails, never a changed exit code) ---------------------------------------

def broken_mail(cfg):
    raise MailError("cannot reach imap.gmail.com: TimeoutError")


def cli_run(config_file, *args, imap=None, mail_factory=None):
    argv = ["--config", str(config_file), *args]
    return cli.main(argv, mail_client_factory=mail_factory or (lambda cfg: imap), providers_factory=providers_factory, smtp_factory=FakeSMTP)


def test_prepare_failing_raises_run_failed_once_a_day_and_a_good_run_resolves_it(config_file, capsys):
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", mail_factory=broken_mail) == 1
    assert subjects() == ["Recruitment alert: prepare failed"] and "TimeoutError" in FakeSMTP.sent[0].get_content()
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", mail_factory=broken_mail) == 1
    assert len(FakeSMTP.sent) == 1                                                  # the same day: no second mail
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP({})) == 0
    assert subjects() == ["Recruitment alert: prepare failed", "Recruitment alert resolved: prepare failed"]
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP({})) == 0
    assert len(FakeSMTP.sent) == 2


def test_a_refused_login_is_one_alert_not_two(config_file):
    def refused(cfg):
        raise MailAuthError("Gmail refused the login; check the app password and that IMAP is enabled")
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", mail_factory=refused) == 1
    assert subjects() == ["Recruitment alert: Gmail IMAP login refused"]


def test_another_run_in_progress_is_exit_2_and_no_alert(config_file):
    from app.pipeline import run_lock
    cfg = load_config(config_file)
    with run_lock(cfg):
        assert cli_run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP({})) == 2
    assert FakeSMTP.sent == [] and FakeSMTP.events == []
    conn = db.connect(cfg["paths"]["database"])
    assert open_keys(conn) == []
    conn.close()


def test_a_prompt_problem_is_exit_3_and_an_alert(config_file):
    with open(config_file, "a", encoding="utf-8") as f:
        f.write("scoring:\n  prompt_file: prompts/does_not_exist.md\n")
    assert cli_run(config_file, "prepare", imap=FakeIMAP({})) == 3
    assert subjects() == ["Recruitment alert: prepare failed"] and "scoring prompt" in FakeSMTP.sent[0].get_content()


def test_a_bad_config_is_exit_3_without_an_alert(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("monitoring:\n  alerts: maybe\n", encoding="utf-8")
    assert cli.main(["--config", str(bad), "status"], smtp_factory=FakeSMTP) == 3
    assert FakeSMTP.events == []                                                    # no valid config, no database: nothing can be recorded


def test_a_crash_is_recorded_and_mailed_and_still_ends_the_program(config_file):
    def crash(cfg):
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        cli.main(["--config", str(config_file), "prepare", "--allow-draft-prompt"], mail_client_factory=lambda c: FakeIMAP({}),
                 providers_factory=crash, smtp_factory=FakeSMTP)
    assert subjects() == ["Recruitment alert: prepare failed"] and "unexpected error: RuntimeError" in FakeSMTP.sent[0].get_content()


def test_a_failing_alert_mail_does_not_change_the_exit_code(config_file):
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("down")] * 9
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", mail_factory=broken_mail) == 1
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    assert open_keys(conn) == ["run_failed:prepare"] and row(conn, "run_failed:prepare")["last_sent"] is None
    conn.close()
    FakeSMTP.failures = []
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", mail_factory=broken_mail) == 1       # the next run delivers it
    assert subjects() == ["Recruitment alert: prepare failed"]


def test_alerts_off_in_the_config_records_and_sends_nothing(config_file):
    with open(config_file, "a", encoding="utf-8") as f:
        f.write("monitoring:\n  alerts: false\n")
    assert cli_run(config_file, "prepare", "--allow-draft-prompt", mail_factory=broken_mail) == 1
    assert FakeSMTP.events == []
    conn = db.connect(load_config(config_file)["paths"]["database"])
    assert open_keys(conn) == ["run_failed:prepare"]
    conn.close()


def prepare_and_score(config_file):
    cli_run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP(four_alerts()))
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    run_id = conn.execute("SELECT id FROM batches").fetchone()[0]
    write_results(cfg, run_id, conn)
    conn.close()
    return cfg


def test_finish_with_a_report_that_cannot_be_sent_raises_run_failed_finish(config_file):
    prepare_and_score(config_file)
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("down")] * 3                 # the report fails, the alert mail goes out
    assert cli_run(config_file, "finish") == 1
    assert subjects() == ["Recruitment alert: finish failed"] and "1 report(s) not sent" in FakeSMTP.sent[0].get_content()
    assert cli_run(config_file, "finish") == 0                                       # the failed report is retried and goes out
    assert len(FakeSMTP.sent) == 3 and "wait for a description" in subjects()[1]                    # alert, the report itself, resolved
    assert subjects()[0] == "Recruitment alert: finish failed" and subjects()[2] == "Recruitment alert resolved: finish failed"


def test_finish_with_a_refused_smtp_login_is_gmail_auth_smtp_and_cannot_be_mailed(config_file):
    cfg = prepare_and_score(config_file)
    FakeSMTP.failures = [smtplib.SMTPAuthenticationError(535, b"bad")] * 2           # the report, then the alert mail
    assert cli_run(config_file, "finish") == 1
    assert FakeSMTP.sent == []
    conn = db.connect(cfg["paths"]["database"])
    assert open_keys(conn) == ["gmail_auth:smtp"] and row(conn, "gmail_auth:smtp")["last_sent"] is None
    assert status(conn, cfg)["open_alerts"][0]["mailed"] is False
    conn.close()
    FakeSMTP.failures = []
    assert cli_run(config_file, "finish") == 0                                       # the password works again: the report goes out
    conn = db.connect(cfg["paths"]["database"])
    assert open_keys(conn) == []                                                     # closed; the user never heard of it, so no mails about it
    conn.close()
    assert not any("alert" in s for s in subjects())


def backups_to_tmp(config_file, tmp_path):
    with open(config_file, "a", encoding="utf-8") as f:
        f.write(f"backup:\n  dir: {(tmp_path / 'backups').as_posix()}\n")


def test_a_backup_that_fails_raises_backup_failed_and_a_good_one_resolves_it(config_file, monkeypatch, tmp_path):
    backups_to_tmp(config_file, tmp_path)
    cli_run(config_file, "status")                                                   # creates the database (schema 15)
    real = backup.create_backup

    def failing(*a, **k):
        raise backup.BackupError(f"copy failed: disk full {PASSWORD}")
    monkeypatch.setattr(backup, "create_backup", failing)
    assert cli_run(config_file, "backup") == 1
    assert subjects() == ["Recruitment alert: backup failed"] and "disk full" in FakeSMTP.sent[0].get_content()
    assert PASSWORD not in FakeSMTP.sent[0].as_string()
    assert cli_run(config_file, "backup") == 1
    assert len(FakeSMTP.sent) == 1                                                   # same day
    monkeypatch.setattr(backup, "create_backup", real)
    assert cli_run(config_file, "backup") == 0
    assert subjects() == ["Recruitment alert: backup failed", "Recruitment alert resolved: backup failed"]


def test_a_failed_backup_never_migrates_the_database(config_file, monkeypatch, tmp_path):
    backups_to_tmp(config_file, tmp_path)
    path = tmp_path / "jobs.db"
    raw = db.sqlite3.connect(str(path))
    for number, script in enumerate(db.MIGRATIONS[:14], start=1):                   # a database from before this version
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    raw.close()
    monkeypatch.setattr(backup, "create_backup", lambda *a, **k: (_ for _ in ()).throw(backup.BackupError("copy failed: disk full")))
    assert cli_run(config_file, "backup") == 1
    check = db.sqlite3.connect(str(path))
    assert check.execute("PRAGMA user_version").fetchone()[0] == 14                  # untouched
    check.close()
    assert subjects() == ["Recruitment alert: backup failed"]                         # mailed directly: there is no table to record it in


def test_a_failed_backup_with_no_database_is_still_mailed(config_file, tmp_path):
    backups_to_tmp(config_file, tmp_path)
    assert cli_run(config_file, "backup") == 1                                       # file not found
    assert subjects() == ["Recruitment alert: backup failed"] and "file not found" in FakeSMTP.sent[0].get_content()


def test_a_backup_alert_with_a_failing_mail_server_does_not_change_the_exit_code(config_file, tmp_path):
    backups_to_tmp(config_file, tmp_path)
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("down")] * 9
    assert cli_run(config_file, "backup") == 1 and FakeSMTP.sent == []
