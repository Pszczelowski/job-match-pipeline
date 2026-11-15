"""jobs.ch job-alert mails: ingestion, dedup, the run, the health alert and the configuration (goal.md 5.1, 5.9, v0.84.0).

Fake IMAP only; the mails are the sanitised copy of the real alert of 12 Nov 2026 and variants of it. The resolver's direct
reading of the posting the mail links (path B) is tested in test_resolver.py, next to the other JobCloud tests.
"""
import email.message
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import alerts, db
from app.config import ConfigError, load_config
from app.mailbox import JOBSCH, SOURCES, ingest_mail
from app.pipeline import status
from tests.test_cli import clean_logger, config_file, run  # noqa: F401  (fixtures)
from tests.test_mailbox import JOBSCH_FOLDER, FakeIMAP, four_alerts, make_mail
from tests.test_pipeline import cfg, conn, providers, run_prepare  # noqa: F401  (fixtures)

FIXTURES = Path(__file__).parent / "fixtures" / "jobsch"
REAL = (FIXTURES / "alert_2026-11-12.html").read_text(encoding="utf-8")
TEXT = (FIXTURES / "alert_2026-11-12.txt").read_text(encoding="utf-8")
SENDER = "jobmail@jobs.ch"
NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
SUBJECT = "3 new job offers found"
UID_1, UID_2 = "60c67166-41c0-4d7a-88e2-c00a9335fa99", "13fcf051-59da-4eda-8899-99dad19c94ee"
EMPTY = "<html><body><p>0 new jobs for your search</p></body></html>"


def jc_mail(html=REAL, message_id="<jc1@x>", date="Thu, 12 Nov 2026 05:40:47 +0000", sender=SENDER, subject=SUBJECT, **kw):
    return make_mail(html, message_id=message_id, sender=sender, date=date, subject=subject, **kw)


def read(conn, cfg, messages, run_id="run1", now=NOW):
    db.start_run(conn, run_id)
    imap = FakeIMAP({}, jobsch=messages)
    return imap, ingest_mail(conn, cfg, imap, run_id, now, source=JOBSCH)


def count(conn, table, where="1 = 1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def open_keys(conn):
    return [a["key"] for a in alerts.open_alerts(conn)]


def test_the_real_alert_through_the_fake_mailbox(conn, cfg):
    imap, r = read(conn, cfg, {"1": jc_mail()})
    assert (r.source, r.mails_seen, r.mails_ok, r.mails_failed, r.found, r.new_jobs) == ("jobsch_alert", 1, 1, 0, 2, 2)
    assert r.errors == [] and ("select", JOBSCH_FOLDER, False) in imap.log and imap.seen() == ["1"]
    assert next(e for e in imap.log if e[0] == "SEARCH")[2:] == ("SINCE", "29-Oct-2026", "FROM", '"jobmail@jobs.ch"')
    row = conn.execute("SELECT source, status, kind, jobs_found, mail_date FROM mail_messages").fetchone()
    assert tuple(row) == ("jobsch_alert", "ok", "alert", 2, "2026-11-12T05:40:47Z")


def test_the_stored_jobs_have_the_uuid_the_clean_url_and_no_description(conn, cfg):
    read(conn, cfg, {"1": jc_mail()})
    rows = conn.execute("SELECT j.title, j.company, j.location_text, j.canonical_url, j.description_missing, j.status, "
                        "s.source, s.external_id, s.url, s.source_date FROM jobs j JOIN job_sources s ON s.job_id = j.id ORDER BY s.id").fetchall()
    assert [r["external_id"] for r in rows] == [UID_1, UID_2]
    first = rows[0]
    assert (first["title"], first["company"], first["location_text"]) == ("Equity Analyst/in Schweizer Aktien (100%)", "schilling partners ag", "Zug")
    assert first["url"] == first["canonical_url"] == f"https://www.jobs.ch/en/vacancies/detail/{UID_1}"        # dedup stores the link without the trailing slash
    assert (first["source"], first["source_date"], first["description_missing"], first["status"]) == ("jobsch_alert", "", 1, "new")
    assert not any("REDACTED" in str(v) or "utm_" in str(v) for r in rows for v in tuple(r))


def test_a_second_run_reads_nothing_again(conn, cfg):
    messages = {"1": jc_mail()}
    read(conn, cfg, messages)
    db.start_run(conn, "run2")
    imap = FakeIMAP({}, jobsch=messages)
    r = ingest_mail(conn, cfg, imap, "run2", NOW, source=JOBSCH)
    assert (r.mails_seen, r.found, r.new_jobs) == (0, 0, 0) and imap.full_fetches() == []
    assert count(conn, "jobs") == 2 and count(conn, "mail_messages") == 1


def test_the_same_jobs_in_a_later_mail_are_not_new_and_not_reposts(conn, cfg):
    _, r = read(conn, cfg, {"1": jc_mail(), "2": jc_mail(message_id="<jc2@x>", date="Fri, 13 Nov 2026 05:30:00 +0000")})
    assert (r.mails_ok, r.found, r.new_jobs) == (2, 4, 2) and r.outcomes == {"new": 2, "unchanged": 2}
    assert count(conn, "jobs") == 2 and count(conn, "jobs", "repost = 1") == 0


def test_the_html_part_of_a_two_part_mail_is_read(conn, cfg):
    msg = email.message.EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"jobs.ch Job Alert <{SENDER}>", "me@example.com", SUBJECT
    msg["Date"], msg["Message-ID"] = "Thu, 12 Nov 2026 05:40:47 +0000", "<jc9@x>"
    msg.set_content(TEXT)
    msg.add_alternative(REAL, subtype="html")
    _, r = read(conn, cfg, {"1": msg.as_bytes()})
    assert (r.mails_ok, r.found) == (1, 2)


def test_mail_from_another_sender_is_recorded_and_not_downloaded(conn, cfg):
    imap, r = read(conn, cfg, {"1": jc_mail(sender="noreply@jobs.ch"), "2": jc_mail(message_id="<jc2@x>")})
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 2) and "not the expected" in r.errors[0]
    assert imap.full_fetches() == ["2"]


def test_a_jobsch_mail_that_is_not_a_job_alert_is_an_error_not_silence(conn, cfg):
    other = "<html><body><h1>Welcome to jobs.ch</h1><p>Thank you for registering</p></body></html>"
    imap, r = read(conn, cfg, {"1": jc_mail(other, "<n1@x>", subject="Welcome"), "2": jc_mail(message_id="<jc2@x>")})
    assert (r.mails_ok, r.mails_failed) == (1, 1) and "not a recognised jobs.ch alert" in r.errors[0] and imap.seen() == ["2"]
    assert tuple(conn.execute("SELECT status, kind, jobs_found FROM mail_messages WHERE message_id = '<n1@x>'").fetchone()) == ("error", "alert", 0)


def test_the_sender_is_taken_from_the_config(conn, cfg):
    cfg["gmail"]["jobsch_sender"] = "other@jobs.ch"
    imap, r = read(conn, cfg, {"1": jc_mail()})
    assert r.mails_failed == 1 and next(e for e in imap.log if e[0] == "SEARCH")[-1] == '"other@jobs.ch"'


def test_jobsch_is_the_fourth_source():
    assert [s.key for s in SOURCES][-1] == "jobsch_alert"
    assert (JOBSCH.name, JOBSCH.adapter_label, JOBSCH.service_notices) == ("jobs.ch", "jobs.ch alert mail", False)


def test_a_missing_jobsch_folder_does_not_stop_the_others(conn, cfg, providers):
    cfg["gmail"]["label_jobsch"] = "no-such-label"
    r = run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    assert r.mail.found == 10 and "jobsch_alert" not in r.mails and "no-such-label" in r.mail_error


def test_jobsch_jobs_are_stored_in_a_run_with_their_own_statistics_row(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, jobsch={"1": jc_mail()}))
    assert r.mails["jobsch_alert"].found == 2 and count(conn, "job_sources", "source = 'jobsch_alert'") == 2
    assert [x[0] for x in conn.execute("SELECT adapter FROM adapter_runs")] == ["UBS alert mail", "jobs.ch alert mail"]


def test_three_empty_jobsch_mails_raise_mail_zero_jobsch_only(conn, cfg, providers):
    messages = {str(100 + i): jc_mail(EMPTY, f"<e{i}@x>", date=f"Sat, {10 + i} Nov 2026 05:30:00 +0000") for i in range(3)}
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, jobsch=messages))
    assert open_keys(conn) == ["mail_zero:jobsch_alert"] and "jobsch_alert" in r.warnings[0] and "contained no jobs" in r.warnings[0]
    s = status(conn, cfg, NOW)
    assert "jobsch_alert" in s["jobsch_mail_warning"] and s["julius_mail_warning"] is None and s["mail_warning"] is None
    assert alerts.short_title("mail_zero:jobsch_alert") == "jobs.ch alert mails contained no jobs"
    assert "jobs.ch" in alerts.HINTS["mail_zero:jobsch_alert"]


def test_status_shows_no_jobsch_warning_when_all_is_fine(conn, cfg):
    assert status(conn, cfg, NOW)["jobsch_mail_warning"] is None


def test_check_mail_checks_the_jobsch_label(config_file, capsys):
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 0
    assert "configured jobs.ch label 'JobSearch/JobSearch/Alerts/JobsCh': found" in capsys.readouterr().out


def test_the_defaults_name_the_label_and_the_sender_of_the_real_mail(cfg):
    assert cfg["gmail"]["label_jobsch"] == "JobSearch/JobSearch/Alerts/JobsCh" and cfg["gmail"]["jobsch_sender"] == "jobmail@jobs.ch"


def test_a_user_config_without_the_jobsch_keys_gets_the_defaults(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text('gmail:\n  label_ubs: "x"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg["gmail"]["label_jobsch"] == "JobSearch/JobSearch/Alerts/JobsCh" and cfg["gmail"]["jobsch_sender"] == "jobmail@jobs.ch"


@pytest.mark.parametrize("key", ["jobsch_sender", "label_jobsch"])
def test_an_empty_jobsch_setting_is_refused(tmp_path, key):
    path = tmp_path / "c.yaml"
    path.write_text(f'gmail:\n  {key}: ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_config(path)


def resolved_job(conn, uid, status="success", posting_id=None, url=None, job_status="new", archived=False):
    """A job of another title that the JobCloud resolver read for the posting `uid` (a resolution row of path A or B)."""
    with conn:
        jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                           "VALUES ('x|other title|' || (SELECT COUNT(*) FROM jobs), 'Other title', 'X', ?, 't', 't')", (job_status,)).lastrowid
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, url, posting_id, status) "
                     "VALUES (?, 't', '2026-11-12', 'T1', 'jobcloud', 'jobcloud', ?, ?, ?)", (jid, url, posting_id, status))
    if archived:
        db.archive_job(conn, jid, "user", "other")
    return jid


def test_a_posting_already_resolved_for_a_job_is_attached_not_created(conn, cfg):
    jid = resolved_job(conn, UID_1, posting_id=UID_1)
    _, r = read(conn, cfg, {"1": jc_mail()})
    assert r.outcomes == {"linked": 1, "new": 1} and r.new_jobs == 1 and count(conn, "jobs") == 2
    assert [tuple(x) for x in conn.execute("SELECT source, external_id FROM job_sources WHERE job_id = ?", (jid,))] == [("jobsch_alert", UID_1)]


def test_the_uuid_is_found_in_the_resolved_url_in_any_case(conn, cfg):
    jid = resolved_job(conn, UID_1, posting_id="not-a-uuid", url=f"https://www.jobs.ch/en/vacancies/detail/{UID_1.upper()}/")
    read(conn, cfg, {"1": jc_mail()})
    assert count(conn, "job_sources", f"job_id = {jid}") == 1 and count(conn, "jobs") == 2


def test_an_upper_case_posting_id_matches(conn, cfg):
    jid = resolved_job(conn, UID_1, posting_id=UID_1.upper())
    read(conn, cfg, {"1": jc_mail()})
    assert count(conn, "job_sources", f"job_id = {jid}") == 1


def test_another_uuid_or_an_unsuccessful_resolution_gives_a_new_job(conn, cfg):
    resolved_job(conn, UID_1, posting_id="0e0e0e0e-0000-4000-8000-000000000000")
    resolved_job(conn, UID_1, status="no_match", posting_id=UID_1)
    _, r = read(conn, cfg, {"1": jc_mail()})
    assert r.outcomes == {"new": 2} and count(conn, "jobs") == 4


def test_a_rejected_job_is_not_brought_back(conn, cfg):
    resolved_job(conn, UID_1, posting_id=UID_1, job_status="rejected")
    resolved_job(conn, UID_2, posting_id=UID_2, archived=True)
    _, r = read(conn, cfg, {"1": jc_mail()})
    assert r.outcomes == {"linked": 1, "archived": 1} and r.new_jobs == 0
    assert count(conn, "jobs") == 1 and count(conn, "job_sources", "source = 'jobsch_alert'") == 1
