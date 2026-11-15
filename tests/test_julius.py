"""Julius Baer job-alert mails: ingestion, dedup, filters, the run and the health alert (goal.md 5.1, 5.9, v0.48.0).

Fake IMAP only; the mails are the sanitised copy of the one real alert (1 Nov 2026) and variants of it.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import alerts, db
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.mailbox import JULIUS, SOURCES, MailError, ingest_mail
from app.models import NormalizedJob
from app.pipeline import status
from tests.test_cli import clean_logger, config_file, run  # noqa: F401  (fixtures)
from tests.test_mailbox import JULIUS_FOLDER, FakeIMAP, four_alerts, make_mail
from tests.test_pipeline import cfg, conn, providers, run_prepare  # noqa: F401  (fixtures)

REAL = (Path(__file__).parent / "fixtures" / "julius" / "alert_2026-11-02.html").read_text(encoding="utf-8")
SENDER = "noreply.hrconnect@juliusbaer.com"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
SUBJECT = "New Job Alert for Zurich"
TITLE = "Account Manager, Turkey, 100% (f/m/d)"
URL = "https://juliusbaer.wd3.myworkdayjobs.com/External/job/Geneva/Account-Manager--Turkey--100---f-m-d-_r-20213-1"


def jb_mail(html=REAL, message_id="<jb1@x>", date="Sun, 01 Nov 2026 19:01:39 -0800", sender=SENDER, subject=SUBJECT, **kw):
    return make_mail(html, message_id=message_id, sender=sender, date=date, subject=subject, **kw)


def multi(n=3):
    """The real alert with n job rows (ids r-20213-1, r-30001-1, ...), built by repeating its row."""
    start = REAL.rfind('<div style="margin:0px auto;max-width:600px;">', 0, REAL.index(f"<p>{TITLE}</p>"))
    end = REAL.index('<div style="margin:0px auto;max-width:600px;">', REAL.index("Apply</span>"))
    rows = "".join(REAL[start:end].replace("r-20213-1", f"r-{30000 + i}-1").replace(TITLE, f"Analyst {i}, Zurich (f/m/d)")
                   .replace("<p>Geneva</p>", "<p>Zurich</p>").replace("/Geneva/", "/Zurich/") for i in range(1, n))
    return REAL[:end] + rows + REAL[end:]


def read(conn, cfg, messages, run_id="run1", now=NOW):
    db.start_run(conn, run_id)
    imap = FakeIMAP({}, julius=messages)
    return imap, ingest_mail(conn, cfg, imap, run_id, now, source=JULIUS)


def count(conn, table, where="1 = 1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def open_keys(conn):
    return [a["key"] for a in alerts.open_alerts(conn)]


# --- ingestion ----------------------------------------------------------------------------------------

def test_the_real_alert_through_the_fake_mailbox(conn, cfg):
    imap, r = read(conn, cfg, {"1": jb_mail()})
    assert (r.source, r.mails_seen, r.mails_ok, r.mails_failed, r.found, r.new_jobs) == ("julius_alert", 1, 1, 0, 1, 1)
    assert r.outcomes == {"new": 1} and r.errors == [] and r.notices == []
    assert ("select", JULIUS_FOLDER, False) in imap.log and imap.seen() == ["1"]
    search = next(e for e in imap.log if e[0] == "SEARCH")
    assert search[2:] == ("SINCE", "19-Oct-2026", "FROM", '"noreply.hrconnect@juliusbaer.com"')
    row = conn.execute("SELECT * FROM mail_messages").fetchone()
    assert (row["source"], row["status"], row["kind"], row["jobs_found"], row["mail_date"]) == ("julius_alert", "ok", "alert", 1, "2026-11-02T03:01:39Z")


def test_the_stored_job_has_the_clean_url_the_id_the_reference_and_the_place(conn, cfg):
    read(conn, cfg, {"1": jb_mail()})
    row = conn.execute("SELECT j.title, j.company, j.location_text, j.canonical_url, j.description_missing, j.status, j.repost, "
                       "s.source, s.external_id, s.reference, s.url, s.source_date, s.message_id "
                       "FROM jobs j JOIN job_sources s ON s.job_id = j.id").fetchone()
    assert (row["title"], row["company"], row["location_text"]) == (TITLE, "Julius Baer", "Geneva")
    assert (row["source"], row["external_id"], row["reference"], row["source_date"], row["message_id"]) == (
        "julius_alert", "r-20213-1", "R-20213", "", "<jb1@x>")
    assert row["url"] == URL == row["canonical_url"]
    assert row["description_missing"] == 1 and row["repost"] == 0 and row["status"] == "new"
    blob = " ".join(str(v) for v in tuple(row))
    assert "shared_id" not in blob and "REDACTED" not in blob


def test_several_jobs_in_one_mail(conn, cfg):
    _, r = read(conn, cfg, {"1": jb_mail(multi(3))})
    assert (r.mails_ok, r.found, r.new_jobs) == (1, 3, 3) and count(conn, "jobs") == 3
    assert [x[0] for x in conn.execute("SELECT external_id FROM job_sources ORDER BY id")] == ["r-20213-1", "r-30001-1", "r-30002-1"]


def test_a_second_run_reads_nothing_again(conn, cfg):
    messages = {"1": jb_mail()}
    read(conn, cfg, messages)
    db.start_run(conn, "run2")
    imap = FakeIMAP({}, julius=messages)
    r = ingest_mail(conn, cfg, imap, "run2", NOW, source=JULIUS)
    assert (r.mails_seen, r.found, r.new_jobs) == (0, 0, 0) and imap.seen() == [] and imap.full_fetches() == []
    assert count(conn, "jobs") == 1 and count(conn, "mail_messages") == 1


def test_the_same_job_in_a_later_digest_is_not_a_repost(conn, cfg):
    _, r = read(conn, cfg, {"1": jb_mail(), "2": jb_mail(multi(2), "<jb2@x>", date="Mon, 02 Nov 2026 05:30:00 +0000")})
    assert (r.mails_ok, r.found, r.new_jobs) == (2, 3, 2) and r.outcomes == {"new": 2, "unchanged": 1}
    assert count(conn, "jobs") == 2 and count(conn, "jobs", "repost = 1") == 0


def test_a_job_that_came_from_another_source_is_linked(conn, cfg):
    db.start_run(conn, "run0")
    first = ingest_job(conn, NormalizedJob(source="six_site", external_id="S-17", title=TITLE, company="Julius Baer",
                                           locations=["Geneva"], description="The full posting text from the employer site."), "run0")
    _, r = read(conn, cfg, {"1": jb_mail()})
    assert r.outcomes == {"linked": 1} and count(conn, "jobs") == 1
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (first.job_id,)).fetchone()
    assert job["repost"] == 0 and job["description_missing"] == 0


def test_mail_from_another_sender_is_recorded_and_not_downloaded(conn, cfg):
    imap, r = read(conn, cfg, {"1": jb_mail(sender="noreply@juliusbaer.com"), "2": jb_mail(message_id="<jb2@x>")})
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 1) and "not the expected" in r.errors[0]
    assert imap.full_fetches() == ["2"]
    assert conn.execute("SELECT status FROM mail_messages WHERE message_id = '<jb1@x>'").fetchone()[0] == "error"


def test_a_julius_mail_that_is_not_a_job_alert_is_an_error_not_silence(conn, cfg):
    other = "<html><body><h1>Thank you for your application</h1><p>We will come back to you</p></body></html>"
    imap, r = read(conn, cfg, {"1": jb_mail(other, "<n1@x>", subject="Application received"), "2": jb_mail(message_id="<jb2@x>")})
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 1) and "not a recognised Julius Baer alert" in r.errors[0]
    assert imap.seen() == ["2"]                           # the odd mail stays unread: the user can look at it
    row = conn.execute("SELECT source, status, kind, jobs_found FROM mail_messages WHERE message_id = '<n1@x>'").fetchone()
    assert tuple(row) == ("julius_alert", "error", "alert", 0)
    imap2 = FakeIMAP({}, julius={"1": jb_mail(other, "<n1@x>")})
    assert ingest_mail(conn, cfg, imap2, "run1", NOW, source=JULIUS).mails_seen == 0         # not retried every day


def test_ubs_expiry_wording_is_not_a_notice_for_julius(conn, cfg):
    _, r = read(conn, cfg, {"1": jb_mail("<html><body>Your search agent will expire in 3 days</body></html>", subject="Search Agent Expiration Warning")})
    assert r.notices == [] and r.mails_failed == 1


def test_plain_text_only_mail(conn, cfg):
    _, r = read(conn, cfg, {"1": jb_mail("just text", plain_only=True)})
    assert r.mails_failed == 1 and "no HTML part" in r.errors[0]


def test_a_bad_row_inside_a_mail_is_reported_but_the_mail_is_ok(conn, cfg):
    html = multi(2).replace("<p>Analyst 1, Zurich (f/m/d)</p>", "<p> </p>")
    _, r = read(conn, cfg, {"1": jb_mail(html)})
    assert (r.mails_ok, r.found, r.new_jobs) == (1, 1, 1) and any("no title" in e for e in r.errors)
    assert "no title" in conn.execute("SELECT error FROM mail_messages").fetchone()[0]


def test_missing_location_is_stored_without_a_place(conn, cfg):
    read(conn, cfg, {"1": jb_mail(REAL.replace("<p>Geneva</p>", ""))})
    assert count(conn, "job_locations") == 0 and count(conn, "jobs") == 1
    assert conn.execute("SELECT location_text FROM jobs").fetchone()[0] is None


def test_the_julius_label_is_taken_from_the_config(conn, cfg):
    cfg["gmail"]["label_julius"] = "no-such-label"
    with pytest.raises(MailError, match="check-mail"):
        ingest_mail(conn, cfg, FakeIMAP({}), "run1", NOW, source=JULIUS)


def test_the_sender_is_taken_from_the_config(conn, cfg):
    cfg["gmail"]["julius_sender"] = "other@juliusbaer.com"
    imap, r = read(conn, cfg, {"1": jb_mail()})
    assert r.mails_failed == 1 and imap.full_fetches() == []
    assert next(e for e in imap.log if e[0] == "SEARCH")[-1] == '"other@juliusbaer.com"'


def test_mail_without_message_id_gets_a_key_with_the_julius_label(conn, cfg):
    read(conn, cfg, {"7": jb_mail(message_id=None)})
    assert conn.execute("SELECT message_id FROM mail_messages").fetchone()[0] == "uid:JobSearch/Alerts/Julius:7"


def test_a_mail_over_the_size_limit_is_skipped(conn, cfg):
    cfg["gmail"]["max_message_bytes"] = 100
    imap, r = read(conn, cfg, {"1": jb_mail()})
    assert r.mails_failed == 1 and "over the limit" in r.errors[0] and imap.full_fetches() == []


def test_with_mark_seen_off_the_mailbox_is_opened_read_only(conn, cfg):
    cfg["gmail"]["mark_seen"] = False
    imap, r = read(conn, cfg, {"1": jb_mail()})
    assert r.mails_ok == 1 and imap.seen() == [] and ("select", JULIUS_FOLDER, True) in imap.log


def test_julius_is_the_third_source():
    assert [s.key for s in SOURCES] == ["ubs_alert", "linkedin_alert", "julius_alert", "jobsch_alert"]
    assert (JULIUS.name, JULIUS.adapter_label, JULIUS.service_notices) == ("Julius Baer", "Julius Baer alert mail", False)


# --- the other sources are unchanged next to it ---------------------------------------------------------

def test_all_three_folders_in_one_run(conn, cfg, providers):
    imap = FakeIMAP(four_alerts(), julius={"201": jb_mail()})
    r = run_prepare(conn, cfg, providers, imap)
    assert set(r.mails) == {"ubs_alert", "linkedin_alert", "julius_alert", "jobsch_alert"} and r.mail is r.mails["ubs_alert"]
    assert (r.mails["julius_alert"].mails_ok, r.mails["julius_alert"].found, r.mails["julius_alert"].new_jobs) == (1, 1, 1)
    assert r.mail_error is None and r.warnings == [] and count(conn, "jobs") + count(conn, "job_archive") == 10
    rows = [tuple(x) for x in conn.execute("SELECT adapter, found, new_jobs, error, warning FROM adapter_runs ORDER BY id")]
    assert rows == [("UBS alert mail", 10, 9, None, None), ("Julius Baer alert mail", 1, 1, None, None)]
    assert imap.log[-1] == ("logout",)


def test_a_quiet_julius_folder_adds_no_row(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    assert [x[0] for x in conn.execute("SELECT adapter FROM adapter_runs")] == ["UBS alert mail"]
    assert r.batch.job_count == 7


def test_julius_jobs_reach_the_batch_without_a_description(conn, cfg, providers):
    zurich = multi(3).replace("<p>Geneva</p>", "<p>Zurich</p>").replace("/Geneva/", "/Zurich/")      # a Geneva job fails the canton rule since v0.92.0
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, julius={"1": jb_mail(zurich)}))
    assert r.filters["fail"] == 0 and r.batch.job_count == 3
    entries = json.loads(r.batch.path.read_text(encoding="utf-8"))["jobs"]
    for e in entries:
        assert e["description"] is None and e["description_missing"] is True and e["posted_on"] is None and e["repost"] is False
        assert [s["source"] for s in e["sources"]] == ["julius_alert"]
        assert e["sources"][0]["url"].startswith("https://juliusbaer.wd3.myworkdayjobs.com/") and "?" not in e["sources"][0]["url"]
        assert next(f for f in e["filter_results"] if f["filter"] == "language")["reason_code"] == "language_not_assessed"


def test_a_job_without_a_place_is_conditional_not_dropped(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, julius={"1": jb_mail(REAL.replace("<p>Geneva</p>", ""))}))
    assert r.filters["fail"] == 0 and r.filters["conditional"] == 1 and r.batch.job_count == 1


def test_a_missing_julius_folder_does_not_stop_the_others(conn, cfg, providers):
    cfg["gmail"]["label_julius"] = "no-such-label"
    r = run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    assert r.mail.found == 10 and "julius_alert" not in r.mails and "linkedin_alert" in r.mails
    assert "no-such-label" in r.mail_error and "check-mail" in r.mail_error and r.mail_login is True
    assert r.batch.job_count == 7
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    row = conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'Julius Baer alert mail'").fetchone()
    assert row and "no-such-label" in row[0]
    assert conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'UBS alert mail'").fetchone()[0] is None


def test_a_missing_ubs_folder_does_not_stop_julius(conn, cfg, providers):
    cfg["gmail"]["label_ubs"] = "no-such-label"
    zurich = REAL.replace("<p>Geneva</p>", "<p>Zurich</p>").replace("/Geneva/", "/Zurich/")      # a Geneva job fails the canton rule since v0.92.0
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, julius={"1": jb_mail(zurich)}))
    assert r.mail is None and r.mails["julius_alert"].found == 1 and "no-such-label" in r.mail_error
    assert r.batch.job_count == 1


def test_all_folders_missing_names_all_three(conn, cfg, providers):
    cfg["gmail"]["label_ubs"], cfg["gmail"]["label_linkedin"], cfg["gmail"]["label_julius"] = "no-ubs", "no-li", "no-jb"
    r = run_prepare(conn, cfg, providers, FakeIMAP({}))
    assert r.mail_error.startswith("UBS: ") and "; LinkedIn: " in r.mail_error and "; Julius Baer: " in r.mail_error and "no-jb" in r.mail_error


# --- the zero-job rule and the alert key, per source ----------------------------------------------------

EMPTY = '<html><body><p>Matching Opportunities:</p><p>See all jobs</p></body></html>'


def empty_digests(n=3, start=10):
    return {str(100 + i): jb_mail(EMPTY, f"<e{i}@x>", date=f"Sat, {start + i} Nov 2026 05:30:00 +0000") for i in range(n)}


def test_three_empty_julius_digests_raise_mail_zero_julius_only(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, julius=empty_digests()))
    assert open_keys(conn) == ["mail_zero:julius_alert"]
    assert len(r.warnings) == 1 and "julius_alert" in r.warnings[0] and "contained no jobs" in r.warnings[0]
    assert "contained no jobs" in conn.execute("SELECT warning FROM adapter_runs WHERE adapter = 'Julius Baer alert mail'").fetchone()[0]
    s = status(conn, cfg, NOW)
    assert "julius_alert" in s["julius_mail_warning"] and s["mail_warning"] is None and s["linkedin_mail_warning"] is None
    assert alerts.short_title("mail_zero:julius_alert") == "Julius Baer alert mails contained no jobs"


def test_status_shows_no_julius_warning_when_all_is_fine(conn, cfg):
    assert status(conn, cfg, NOW)["julius_mail_warning"] is None


def test_a_digest_with_jobs_ends_the_julius_alert(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP({}, julius=empty_digests()))
    assert open_keys(conn) == ["mail_zero:julius_alert"]
    messages = {"200": jb_mail(REAL, "<good@x>", date="Wed, 14 Nov 2026 05:30:00 +0000")}
    run_prepare(conn, cfg, providers, FakeIMAP({}, julius=messages), "run2", NOW + timedelta(days=1))
    assert open_keys(conn) == []


def test_unreadable_julius_digests_raise_it_too(conn, cfg, providers):
    junk = {str(100 + i): jb_mail("<html><body>newsletter</body></html>", f"<j{i}@x>", date=f"Sat, {10 + i} Nov 2026 05:30:00 +0000") for i in range(3)}
    r = run_prepare(conn, cfg, providers, FakeIMAP({}, julius=junk))
    assert open_keys(conn) == ["mail_zero:julius_alert"] and "could not be read" in r.warnings[0]
    assert r.mail_error is None


def test_the_sources_have_their_own_alert_keys(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts(), julius=empty_digests()))
    assert open_keys(conn) == ["mail_zero:julius_alert"]


def test_a_run_that_cannot_open_the_julius_folder_leaves_its_alert_alone(conn, cfg, providers):
    alerts.observe(conn, cfg, "mail_zero:julius_alert", "x", NOW)
    cfg["gmail"]["label_julius"] = "no-such-label"
    run_prepare(conn, cfg, providers, FakeIMAP({}))
    assert open_keys(conn) == ["mail_zero:julius_alert"]


def test_the_alert_mail_for_julius_names_julius_baer(cfg):
    row = {"key": "mail_zero:julius_alert", "message": "the last 3 alert mails of julius_alert contained no jobs",
           "first_seen": "2026-11-12T05:00:00Z", "count": 1}
    msg = alerts.raised_message(cfg, row)
    body = msg.get_content()
    assert "Julius Baer alert mails contained no jobs" in msg["Subject"] and "Julius Baer" in body and "saved search at UBS" not in body


# --- command line and configuration ---------------------------------------------------------------------

def test_prepare_prints_the_totals_of_all_sources(config_file, capsys):
    imap = FakeIMAP(four_alerts(), julius={"201": jb_mail()})
    assert run(config_file, "prepare", "--allow-draft-prompt", imap=imap) == 0
    out = capsys.readouterr().out
    assert "mails 5 ok / 0 failed" in out and "jobs found 11, new 10" in out


def test_check_mail_checks_all_three_labels(config_file, capsys):
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 0
    out = capsys.readouterr().out
    assert "configured Julius Baer label 'JobSearch/Alerts/Julius': found" in out and "NOT FOUND" not in out


def test_check_mail_flags_a_missing_julius_label(config_file, capsys):
    config_file.write_text(config_file.read_text(encoding="utf-8") + "gmail:\n  label_julius: other-label\n", encoding="utf-8")
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 1
    out = capsys.readouterr().out
    assert "configured Julius Baer label 'other-label': NOT FOUND" in out and "configured UBS label 'JobSearch/JobSearch/Alerts/UBS': found" in out


def test_the_defaults_name_the_verified_label_and_the_sender_of_the_real_mail(cfg):
    assert cfg["gmail"]["label_julius"] == "JobSearch/Alerts/Julius"          # not under JobSearch/JobSearch/
    assert cfg["gmail"]["julius_sender"] == "noreply.hrconnect@juliusbaer.com"


def test_a_user_config_without_the_julius_keys_gets_the_defaults(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text('gmail:\n  label_ubs: "x"\n  lookback_days: 3\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg["gmail"]["label_julius"] == "JobSearch/Alerts/Julius" and cfg["gmail"]["julius_sender"] == "noreply.hrconnect@juliusbaer.com"


@pytest.mark.parametrize("key", ["julius_sender", "label_julius"])
def test_an_empty_julius_setting_is_refused(tmp_path, key):
    path = tmp_path / "c.yaml"
    path.write_text(f'gmail:\n  {key}: ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_config(path)


def test_no_code_fetches_julius_baer_pages():
    # only the employer adapters name a Workday host: app/adapters/julius.py (the user's decision of 2 Nov 2026, goal.md 5.1) and,
    # since v0.70.0 (requirement changed 3 Nov 2026: Workday readers), app/adapters/workday.py (host names in its tenant list);
    # "juliusbaer" stays in the julius.py files only
    root = Path(__file__).parent.parent / "app"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "julius.py" and path.relative_to(root).as_posix() != "adapters/workday.py":
            assert "myworkdayjobs" not in text, path
        if path.name != "julius.py":
            assert "juliusbaer" not in text, path
    julius = (root / "parsers" / "julius.py").read_text(encoding="utf-8")
    assert "urlopen" not in julius and "requests" not in julius and "urllib.request" not in julius
