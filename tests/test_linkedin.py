"""LinkedIn job-alert mails: ingestion, dedup, filters, the run and the health alert (goal.md 5.1, 5.9, v0.38.0).

Fake IMAP only; the mails are the sanitised copy of the one real alert (1 Nov 2026) and variants of it.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import alerts, db
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.mailbox import LINKEDIN, MailError, ingest_mail
from app.models import NormalizedJob
from app.pipeline import status
from tests.test_cli import clean_logger, config_file, run  # noqa: F401  (fixtures)
from tests.test_mailbox import JOBSCH_FOLDER, JULIUS_FOLDER, LINKEDIN_FOLDER, FakeIMAP, four_alerts, make_mail
from tests.test_pipeline import cfg, conn, providers, run_prepare  # noqa: F401  (fixtures)

REAL = (Path(__file__).parent / "fixtures" / "linkedin" / "alert_2026-11-01.html").read_text(encoding="utf-8")
SENDER = "jobalerts-noreply@linkedin.com"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
IDS = ["4441286658", "4477598320", "4475607540", "4485175120", "4483959119"]
SUBJECT = "Deutsche Bank is hiring a Business Transformation Specialist - Data & Analytics"


def li_mail(html=REAL, message_id="<li1@x>", date="Sun, 01 Nov 2026 13:30:56 +0000", sender=SENDER, subject=SUBJECT, **kw):
    return make_mail(html, message_id=message_id, sender=sender, date=date, subject=subject, **kw)


def read(conn, cfg, messages, run_id="run1", now=NOW):
    db.start_run(conn, run_id)
    imap = FakeIMAP({}, linkedin=messages)
    return imap, ingest_mail(conn, cfg, imap, run_id, now, source=LINKEDIN)


def count(conn, table, where="1 = 1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def open_keys(conn):
    return [a["key"] for a in alerts.open_alerts(conn)]


# --- ingestion ----------------------------------------------------------------------------------------

def test_the_real_alert_through_the_fake_mailbox(conn, cfg):
    imap, r = read(conn, cfg, {"1": li_mail()})
    assert (r.source, r.mails_seen, r.mails_ok, r.mails_failed, r.found, r.new_jobs) == ("linkedin_alert", 1, 1, 0, 5, 5)
    assert r.outcomes == {"new": 5} and r.errors == [] and r.notices == []
    assert ("select", LINKEDIN_FOLDER, False) in imap.log and imap.seen() == ["1"]
    search = next(e for e in imap.log if e[0] == "SEARCH")
    assert search[2:] == ("SINCE", "19-Oct-2026", "FROM", '"jobalerts-noreply@linkedin.com"')
    assert count(conn, "jobs") == 5
    row = conn.execute("SELECT * FROM mail_messages").fetchone()
    assert (row["source"], row["status"], row["kind"], row["jobs_found"], row["mail_date"]) == ("linkedin_alert", "ok", "alert", 5, "2026-11-01T13:30:56Z")


def test_stored_jobs_have_the_clean_url_the_job_id_and_the_place(conn, cfg):
    read(conn, cfg, {"1": li_mail()})
    rows = conn.execute("SELECT j.title, j.company, j.location_text, j.canonical_url, j.description_missing, j.status, j.repost, "
                        "s.source, s.external_id, s.url, s.source_date, s.message_id, s.raw "
                        "FROM jobs j JOIN job_sources s ON s.job_id = j.id ORDER BY j.id").fetchall()
    assert [r["external_id"] for r in rows] == IDS
    first = rows[0]
    assert (first["title"], first["company"], first["location_text"]) == ("Business Transformation Specialist - Data & Analytics", "Deutsche Bank", "Zurich")
    assert first["canonical_url"] == "https://www.linkedin.com/jobs/view/4441286658" == first["url"]
    assert (first["source"], first["source_date"], first["message_id"], first["status"]) == ("linkedin_alert", "", "<li1@x>", "new")
    assert all(r["description_missing"] == 1 and r["repost"] == 0 for r in rows)
    assert json.loads(first["raw"])["work_mode"] == "Hybrid"
    assert rows[4]["location_text"] == "Wallisellen / Hof"
    blob = " ".join(str(v) for r in rows for v in tuple(r))
    assert "trackingId" not in blob and "REDACTED" not in blob and "otpToken" not in blob


def test_a_second_run_reads_nothing_again(conn, cfg):
    messages = {"1": li_mail()}
    read(conn, cfg, messages)
    db.start_run(conn, "run2")
    imap = FakeIMAP({}, linkedin=messages)
    r = ingest_mail(conn, cfg, imap, "run2", NOW, source=LINKEDIN)
    assert (r.mails_seen, r.found, r.new_jobs) == (0, 0, 0) and imap.seen() == [] and imap.full_fetches() == []
    assert count(conn, "jobs") == 5 and count(conn, "mail_messages") == 1


def test_the_same_jobs_in_a_later_digest_are_not_a_repost(conn, cfg):
    later = REAL.replace("4483959119", "4483959999").replace("Oracle Business Consultant", "Oracle Solution Architect")
    imap, r = read(conn, cfg, {"1": li_mail(), "2": li_mail(later, "<li2@x>", date="Mon, 02 Nov 2026 05:30:00 +0000")})
    assert (r.mails_ok, r.found, r.new_jobs) == (2, 10, 6)
    assert r.outcomes == {"new": 6, "unchanged": 4}
    assert count(conn, "jobs") == 6 and count(conn, "jobs", "repost = 1") == 0 and count(conn, "job_sources") == 6


def test_a_job_that_came_from_another_source_is_linked_without_a_repost_flag(conn, cfg):
    db.start_run(conn, "run0")
    first = ingest_job(conn, NormalizedJob(source="six_site", external_id="S-17", title="Senior IT-Business Analyst", company="yellowshark AG",
                                           locations=["Zurich"], description="The full posting text from the employer site."), "run0")
    assert first.outcome == "new"
    _, r = read(conn, cfg, {"1": li_mail()})
    assert r.outcomes == {"new": 4, "linked": 1} and count(conn, "jobs") == 5
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (first.job_id,)).fetchone()
    assert job["repost"] == 0 and job["description"].startswith("The full posting") and job["description_missing"] == 0
    assert [r["source"] for r in conn.execute("SELECT source FROM job_sources WHERE job_id = ? ORDER BY id", (first.job_id,))] == ["six_site", "linkedin_alert"]


def test_a_linkedin_job_that_is_found_later_on_an_employer_site_is_linked_too(conn, cfg):
    read(conn, cfg, {"1": li_mail()})
    out = ingest_job(conn, NormalizedJob(source="six_site", external_id="S-17", title="Senior IT-Business Analyst", company="yellowshark AG",
                                         locations=["Zurich"], description="Employer text."), "run2")
    assert out.outcome == "linked" and out.repost is False and count(conn, "jobs") == 5
    row = conn.execute("SELECT repost, description, description_missing FROM jobs WHERE id = ?", (out.job_id,)).fetchone()
    assert tuple(row) == (0, "Employer text.", 0)


def test_mail_from_another_sender_is_recorded_and_not_downloaded(conn, cfg):
    imap, r = read(conn, cfg, {"1": li_mail(sender="invitations@linkedin.com"), "2": li_mail(message_id="<li2@x>")})
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 5) and "not the expected" in r.errors[0]
    assert imap.full_fetches() == ["2"]
    assert conn.execute("SELECT status FROM mail_messages WHERE message_id = '<li1@x>'").fetchone()[0] == "error"


def test_a_linkedin_mail_that_is_not_a_job_alert_is_an_error_not_silence(conn, cfg):
    other = "<html><body><h1>You appeared in 4 searches this week</h1><p>See who's looking</p></body></html>"
    imap, r = read(conn, cfg, {"1": li_mail(other, "<n1@x>", subject="You appeared in 4 searches"), "2": li_mail(message_id="<li2@x>")})
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 5) and "not a recognised LinkedIn alert" in r.errors[0]
    assert imap.seen() == ["2"]                           # the odd mail stays unread: the user can look at it
    row = conn.execute("SELECT source, status, kind, jobs_found, error FROM mail_messages WHERE message_id = '<n1@x>'").fetchone()
    assert (row["source"], row["status"], row["kind"], row["jobs_found"]) == ("linkedin_alert", "error", "alert", 0)
    imap2 = FakeIMAP({}, linkedin={"1": li_mail(other, "<n1@x>")})
    assert ingest_mail(conn, cfg, imap2, "run1", NOW, source=LINKEDIN).mails_seen == 0         # not retried every day


def test_ubs_expiry_wording_is_not_a_notice_for_linkedin(conn, cfg):
    _, r = read(conn, cfg, {"1": li_mail("<html><body>Your search agent will expire in 3 days</body></html>", subject="Search Agent Expiration Warning")})
    assert r.notices == [] and r.mails_failed == 1


def test_plain_text_only_mail(conn, cfg):
    _, r = read(conn, cfg, {"1": li_mail("just text", plain_only=True)})
    assert r.mails_failed == 1 and "no HTML part" in r.errors[0]


def test_a_bad_card_inside_a_mail_is_reported_but_the_mail_is_ok(conn, cfg):
    html = REAL.replace("Rocken® &middot; Effretikon", "&middot; Effretikon")
    _, r = read(conn, cfg, {"1": li_mail(html)})
    assert (r.mails_ok, r.found, r.new_jobs) == (1, 4, 4) and any("no company" in e for e in r.errors)
    assert "no company" in conn.execute("SELECT error FROM mail_messages").fetchone()[0]


def test_missing_location_is_stored_without_a_place(conn, cfg):
    html = REAL.replace("Aargauische Kantonalbank &middot; Aarau", "Aargauische Kantonalbank")
    read(conn, cfg, {"1": li_mail(html)})
    assert count(conn, "job_locations") == 4
    assert conn.execute("SELECT location_text FROM jobs WHERE company = 'Aargauische Kantonalbank'").fetchone()[0] is None


def test_the_linkedin_label_is_taken_from_the_config(conn, cfg):
    cfg["gmail"]["label_linkedin"] = "no-such-label"
    with pytest.raises(MailError, match="check-mail"):
        ingest_mail(conn, cfg, FakeIMAP({}), "run1", NOW, source=LINKEDIN)


def test_the_sender_is_taken_from_the_config(conn, cfg):
    cfg["gmail"]["linkedin_sender"] = "other@linkedin.com"
    imap, r = read(conn, cfg, {"1": li_mail()})
    assert r.mails_failed == 1 and imap.full_fetches() == []
    assert next(e for e in imap.log if e[0] == "SEARCH")[-1] == '"other@linkedin.com"'


def test_mail_without_message_id_gets_a_key_with_the_linkedin_label(conn, cfg):
    read(conn, cfg, {"7": li_mail(message_id=None)})
    assert conn.execute("SELECT message_id FROM mail_messages").fetchone()[0] == "uid:JobSearch/JobSearch/Alerts/LinkedIn:7"


# --- UBS is unchanged next to it -----------------------------------------------------------------------

def test_both_folders_in_one_run(conn, cfg, providers):
    imap = FakeIMAP(four_alerts(), linkedin={"101": li_mail()})
    r = run_prepare(conn, cfg, providers, imap)
    assert set(r.mails) == {"ubs_alert", "linkedin_alert", "julius_alert", "jobsch_alert"} and r.mail is r.mails["ubs_alert"]
    assert (r.mail.mails_ok, r.mail.found, r.mail.new_jobs) == (4, 10, 9)
    assert (r.mails["linkedin_alert"].mails_ok, r.mails["linkedin_alert"].found, r.mails["linkedin_alert"].new_jobs) == (1, 5, 5)
    assert r.mail_error is None and r.warnings == [] and count(conn, "jobs") + count(conn, "job_archive") == 14
    rows = [tuple(x) for x in conn.execute("SELECT adapter, found, new_jobs, error, warning FROM adapter_runs ORDER BY id")]
    assert rows == [("UBS alert mail", 10, 9, None, None), ("LinkedIn alert mail", 5, 5, None, None)]
    assert imap.log[-1] == ("logout",)
    assert {e[1] for e in imap.log if e[0] == "select"} == {'"JobSearch/JobSearch/Alerts/UBS"', LINKEDIN_FOLDER, JULIUS_FOLDER, JOBSCH_FOLDER}


def test_a_quiet_linkedin_folder_adds_no_row_and_ubs_is_as_before(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    assert [x[0] for x in conn.execute("SELECT adapter FROM adapter_runs")] == ["UBS alert mail"]
    assert r.filters == {"pass": 7, "conditional": 0, "fail": 2} and r.batch.job_count == 7


def test_linkedin_jobs_reach_the_batch_without_a_description(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin={"1": li_mail()}))
    assert r.filters["fail"] == 0 and r.batch.job_count == 5
    entries = json.loads(r.batch.path.read_text(encoding="utf-8"))["jobs"]
    assert len(entries) == 5
    for e in entries:
        assert e["description"] is None and e["description_missing"] is True and e["posted_on"] is None and e["repost"] is False
        assert [s["source"] for s in e["sources"]] == ["linkedin_alert"]
        assert e["sources"][0]["url"].startswith("https://www.linkedin.com/jobs/view/") and "?" not in e["sources"][0]["url"]
        language = next(f for f in e["filter_results"] if f["filter"] == "language")
        assert (language["outcome"], language["reason_code"]) == ("pass", "language_not_assessed")
        assert next(f for f in e["filter_results"] if f["filter"] == "seniority")["outcome"] == "pass"


def test_seniority_is_decided_by_the_title_patterns_for_linkedin_jobs(conn, cfg, providers):
    html = (REAL.replace("Senior IT-Business Analyst", "Head of IT Business Analysis")
            .replace("ERP Application Manager (m/w/d)", "Vice President Finance Systems")
            .replace("Oracle Business Consultant / Berater für Digitalisierte Geschäftsprozesse (m/w/d)", "Associate Director Reporting"))
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin={"1": li_mail(html)}))
    assert r.filters["fail"] == 2 and r.batch.job_count == 3
    archived = {(row["title"], row["reason_code"]) for row in conn.execute("SELECT title, reason_code FROM job_archive")}
    assert archived == {("Head of IT Business Analysis", "seniority_title_pattern"), ("Vice President Finance Systems", "seniority_title_pattern")}
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE title = 'Associate Director Reporting'").fetchone()[0] == 1


def test_an_archived_linkedin_job_is_not_new_again(conn, cfg, providers):
    html = REAL.replace("Senior IT-Business Analyst", "Head of IT Business Analysis")
    run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin={"1": li_mail(html)}), "run1")
    again = FakeIMAP({}, linkedin={"1": li_mail(html), "2": li_mail(html, "<li2@x>", date="Mon, 02 Nov 2026 05:30:00 +0000")})
    r = run_prepare(conn, cfg, providers, again, "run2", NOW + timedelta(days=1))
    assert r.mails["linkedin_alert"].outcomes == {"unchanged": 4, "archived": 1}
    assert count(conn, "job_archive") == 1


def test_a_missing_linkedin_folder_does_not_stop_ubs(conn, cfg, providers):
    cfg["gmail"]["label_linkedin"] = "no-such-label"
    r = run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    assert r.mail.found == 10 and "linkedin_alert" not in r.mails
    assert "no-such-label" in r.mail_error and "check-mail" in r.mail_error and r.mail_login is True
    assert r.batch.job_count == 7
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    row = conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'LinkedIn alert mail'").fetchone()
    assert row and "no-such-label" in row[0]
    assert conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'UBS alert mail'").fetchone()[0] is None


def test_a_missing_ubs_folder_does_not_stop_linkedin(conn, cfg, providers):
    cfg["gmail"]["label_ubs"] = "no-such-label"
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin={"1": li_mail()}))
    assert r.mail is None and r.mails["linkedin_alert"].found == 5 and "no-such-label" in r.mail_error
    assert r.batch.job_count == 5


def test_both_folders_missing_names_both(conn, cfg, providers):
    cfg["gmail"]["label_ubs"], cfg["gmail"]["label_linkedin"] = "no-ubs", "no-li"
    r = run_prepare(conn, cfg, providers, FakeIMAP({}))
    assert r.mail_error.startswith("UBS: ") and "; LinkedIn: " in r.mail_error and "no-ubs" in r.mail_error and "no-li" in r.mail_error


# --- the zero-job rule and the alert key, per source ----------------------------------------------------

EMPTY = ('<html><body><table><tr><td><h1 data-test-id="email-header"><a href="https://www.linkedin.com/comm/jobs/search-results/">'
         'Your job alert for <strong>requirements analyst</strong></a></h1></td></tr></table></body></html>')


def empty_digests(n=3, start=10):
    return {str(100 + i): li_mail(EMPTY, f"<e{i}@x>", date=f"Sat, {start + i} Nov 2026 05:30:00 +0000") for i in range(n)}


def test_three_empty_linkedin_digests_raise_mail_zero_linkedin_only(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin=empty_digests()))
    assert open_keys(conn) == ["mail_zero:linkedin_alert"]
    assert len(r.warnings) == 1 and "linkedin_alert" in r.warnings[0] and "contained no jobs" in r.warnings[0]
    assert "contained no jobs" in conn.execute("SELECT warning FROM adapter_runs WHERE adapter = 'LinkedIn alert mail'").fetchone()[0]
    s = status(conn, cfg, NOW)
    assert "linkedin_alert" in s["linkedin_mail_warning"] and s["mail_warning"] is None
    assert alerts.short_title("mail_zero:linkedin_alert") == "LinkedIn alert mails contained no jobs"
    assert alerts.short_title("mail_zero:ubs_alert") == "UBS alert mails contained no jobs"


def test_a_digest_with_jobs_ends_the_linkedin_alert(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin=empty_digests()))
    assert open_keys(conn) == ["mail_zero:linkedin_alert"]
    messages = {"200": li_mail(REAL, "<good@x>", date="Wed, 14 Nov 2026 05:30:00 +0000")}
    run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin=messages), "run2", NOW + timedelta(days=1))
    assert open_keys(conn) == []


def test_unreadable_linkedin_digests_raise_it_too(conn, cfg, providers):
    junk = {str(100 + i): li_mail("<html><body>newsletter</body></html>", f"<j{i}@x>", date=f"Sat, {10 + i} Nov 2026 05:30:00 +0000") for i in range(3)}
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, linkedin=junk))
    assert open_keys(conn) == ["mail_zero:linkedin_alert"] and "could not be read" in r.warnings[0]
    assert r.mail_error is None                           # unreadable mails are not a failed run, as for UBS


def test_the_two_sources_have_their_own_alert_keys(conn, cfg, providers):
    ubs_html = "These jobs matched the criteria specified for: Zurich <br><table><tr><th>Title</th><th>Job Reference #</th></tr></table>"
    ubs_empty = {str(n): make_mail(ubs_html, f"<z{n}@x>", date=f"Sat, 1{n} Nov 2026 03:00:00 +0000") for n in range(1, 4)}
    run_prepare(conn, cfg, providers, FakeIMAP(ubs_empty, linkedin={"101": li_mail()}))
    assert open_keys(conn) == ["mail_zero:ubs_alert"]


def test_a_run_that_cannot_open_the_linkedin_folder_leaves_its_alert_alone(conn, cfg, providers):
    alerts.observe(conn, cfg, "mail_zero:linkedin_alert", "x", NOW)
    cfg["gmail"]["label_linkedin"] = "no-such-label"
    run_prepare(conn, cfg, providers, FakeIMAP({}))
    assert open_keys(conn) == ["mail_zero:linkedin_alert"]


def test_the_alert_mail_for_linkedin_names_linkedin(cfg):
    row = {"key": "mail_zero:linkedin_alert", "message": "the last 3 alert mails of linkedin_alert contained no jobs",
           "first_seen": "2026-11-12T05:00:00Z", "count": 1}
    msg = alerts.raised_message(cfg, row)
    body = msg.get_content()
    assert "LinkedIn alert mails contained no jobs" in msg["Subject"] and "LinkedIn" in body and "saved search at UBS" not in body
    ubs = alerts.raised_message(cfg, dict(row, key="mail_zero:ubs_alert")).get_content()
    assert "saved search at UBS" in ubs


# --- command line and configuration ---------------------------------------------------------------------

def test_prepare_prints_the_totals_of_both_sources(config_file, capsys):
    imap = FakeIMAP(four_alerts(), linkedin={"101": li_mail()})
    assert run(config_file, "prepare", "--allow-draft-prompt", imap=imap) == 0
    out = capsys.readouterr().out
    assert "mails 5 ok / 0 failed" in out and "jobs found 15, new 14" in out


def test_check_mail_checks_both_labels(config_file, capsys):
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 0
    out = capsys.readouterr().out
    assert "configured UBS label" in out and "configured LinkedIn label" in out and "NOT FOUND" not in out


def test_check_mail_flags_a_missing_linkedin_label(config_file, capsys):
    config_file.write_text(config_file.read_text(encoding="utf-8") + "gmail:\n  label_linkedin: other-label\n", encoding="utf-8")
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 1
    out = capsys.readouterr().out
    assert "configured LinkedIn label 'other-label': NOT FOUND" in out and "configured UBS label 'JobSearch/JobSearch/Alerts/UBS': found" in out


def test_the_defaults_name_the_verified_label_and_the_sender_of_the_real_mail(cfg):
    assert cfg["gmail"]["label_linkedin"] == "JobSearch/JobSearch/Alerts/LinkedIn"
    assert cfg["gmail"]["linkedin_sender"] == "jobalerts-noreply@linkedin.com"


def test_an_empty_linkedin_sender_is_refused(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text('gmail:\n  linkedin_sender: ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="linkedin_sender"):
        load_config(path)
