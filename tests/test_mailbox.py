import email.message
import imaplib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import db, mailbox
from app.config import load_config
from app.mailbox import MailError, ingest_mail, list_folders, search_agent_warning, zero_job_warning

UBS = Path(__file__).parent / "fixtures" / "ubs"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
SENDER = "agentserver@ent.brassring.com"


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    return load_config()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


def make_mail(html, message_id="<m1@x>", sender=SENDER, date="Sat, 24 Oct 2026 03:00:00 +0100", subject="Search agent results",
              plain_only=False):
    msg = email.message.EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = f"UBS <{sender}>", "me@example.com", subject, date
    if message_id:
        msg["Message-ID"] = message_id
    if plain_only:
        msg.set_content(html)
    else:
        msg.set_content(html, subtype="html", cte="base64")
    return msg.as_bytes()


def fixture_mail(name, message_id, **kw):
    return make_mail((UBS / name).read_text(encoding="utf-8"), message_id=message_id, **kw)


UBS_FOLDER = '"JobSearch/JobSearch/Alerts/UBS"'
LINKEDIN_FOLDER = '"JobSearch/JobSearch/Alerts/LinkedIn"'
JULIUS_FOLDER = '"JobSearch/Alerts/Julius"'
JOBSCH_FOLDER = '"JobSearch/JobSearch/Alerts/JobsCh"'


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for ingest_mail. Three folders, as in the real mailbox: the UBS label (the
    `messages`), the LinkedIn label (`linkedin`) and the Julius Baer label (`julius`) and the jobs.ch label (`jobsch`), the last three empty unless given. Message uids must differ between the folders."""

    def __init__(self, messages, folder=UBS_FOLDER, linkedin=None, linkedin_folder=LINKEDIN_FOLDER, julius=None, julius_folder=JULIUS_FOLDER,
                 jobsch=None, jobsch_folder=JOBSCH_FOLDER):
        self.boxes = {folder: dict(messages), linkedin_folder: dict(linkedin or {}), julius_folder: dict(julius or {}), jobsch_folder: dict(jobsch or {})}      # folder -> uid (str) -> raw bytes
        self.folder = folder
        self.current = None
        self.log = []
        self.fail_download = set()

    @property
    def messages(self):
        return self.boxes[self.folder]

    def select(self, name, readonly=False):
        self.log.append(("select", name, readonly))
        if name not in self.boxes:
            return "NO", [b"no such folder"]
        self.current = name
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.log.append((command, *args))
        box = self.boxes[self.current]
        if command == "SEARCH":
            return "OK", [b" ".join(u.encode() for u in box)]
        if command == "FETCH":
            uid, spec = args
            raw = box[uid]
            if "HEADER.FIELDS" in spec:
                head = raw.split(b"\n\n", 1)[0] + b"\r\n\r\n"
                return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)} BODY[HEADER.FIELDS (...)] {{{len(head)}}}".encode(), head), b")"]
            if uid in self.fail_download:
                return "NO", [b"error"]
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            return "OK", [b"done"]
        raise AssertionError(command)

    def seen(self):
        return [e[1] for e in self.log if e[0] == "STORE"]

    def full_fetches(self):
        return [e[1] for e in self.log if e[0] == "FETCH" and "HEADER.FIELDS" not in e[2]]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "JobSearch/JobSearch/Alerts/UBS"',
                      b'(\\HasNoChildren) "/" "JobSearch/JobSearch/Alerts/LinkedIn"', b'(\\HasNoChildren) "/" "JobSearch/Alerts/Julius"',
                      b'(\\HasNoChildren) "/" "JobSearch/JobSearch/Alerts/JobsCh"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"', b'(\\HasNoChildren) "/" Drafts']

    def logout(self):
        self.log.append(("logout",))


def four_alerts():
    names = sorted(p.name for p in UBS.glob("*.html"))
    return {str(i + 1): fixture_mail(n, f"<{n}@x>", date=f"Sat, 2{4 + i} Oct 2026 03:00:00 +0000") for i, n in enumerate(names)}


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- the real alerts through a fake mailbox ---------------------------------------------------------

def test_four_real_alerts_give_nine_jobs_and_one_repost(conn, cfg):
    imap = FakeIMAP(four_alerts())
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert (r.mails_seen, r.mails_ok, r.mails_failed, r.found, r.new_jobs) == (4, 4, 0, 10, 9)
    assert r.outcomes == {"new": 9, "seen_again": 1}
    assert count(conn, "jobs") == 9 and count(conn, "mail_messages") == 4
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE repost = 1").fetchone()[0] == 1
    assert sorted(imap.seen()) == ["1", "2", "3", "4"]


def test_a_second_run_reads_nothing_again(conn, cfg):
    messages = four_alerts()
    ingest_mail(conn, cfg, FakeIMAP(messages), "run1", NOW)
    imap = FakeIMAP(messages)
    r = ingest_mail(conn, cfg, imap, "run2", NOW)
    assert (r.mails_seen, r.found, r.new_jobs) == (0, 0, 0) and imap.seen() == [] and imap.full_fetches() == []
    assert count(conn, "jobs") == 9


def test_search_is_limited_by_label_date_and_sender(conn, cfg):
    imap = FakeIMAP({})
    ingest_mail(conn, cfg, imap, "run1", NOW)
    assert ("select", '"JobSearch/JobSearch/Alerts/UBS"', False) in imap.log
    search = next(e for e in imap.log if e[0] == "SEARCH")
    assert search[2:] == ("SINCE", "19-Oct-2026", "FROM", '"agentserver@ent.brassring.com"')       # 14 days before 2 Nov


def test_mark_seen_off_reads_only(conn, cfg):
    cfg["gmail"]["mark_seen"] = False
    imap = FakeIMAP(four_alerts())
    ingest_mail(conn, cfg, imap, "run1", NOW)
    assert imap.seen() == [] and ("select", '"JobSearch/JobSearch/Alerts/UBS"', True) in imap.log
    assert count(conn, "jobs") == 9


def test_the_mail_record_stores_date_and_counts(conn, cfg):
    ingest_mail(conn, cfg, FakeIMAP(four_alerts()), "run1", NOW)
    row = conn.execute("SELECT * FROM mail_messages WHERE message_id = ?", ("<alert_2026-10-24.html@x>",)).fetchone()
    assert (row["status"], row["jobs_found"], row["run_id"], row["mail_date"]) == ("ok", 2, "run1", "2026-10-24T03:00:00Z")


# --- bad mails never stop the others ---------------------------------------------------------------

def test_wrong_sender_is_recorded_and_ignored(conn, cfg):
    imap = FakeIMAP({"1": fixture_mail("alert_2026-10-30.html", "<a@x>", sender="phisher@evil.example"),
                     "2": fixture_mail("alert_2026-10-28.html", "<b@x>")})
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 1) and "not the expected" in r.errors[0]
    assert imap.full_fetches() == ["2"]            # the mail from the wrong sender was never downloaded
    assert conn.execute("SELECT status FROM mail_messages WHERE message_id = '<a@x>'").fetchone()[0] == "error"


def test_unrecognised_mail_is_an_error(conn, cfg):
    imap = FakeIMAP({"1": make_mail("<html><body>Your weekly newsletter</body></html>", "<a@x>"),
                     "2": fixture_mail("alert_2026-10-28.html", "<b@x>")})
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 1, 1) and "not a recognised UBS alert" in r.errors[0]
    assert imap.seen() == ["2"]                    # a mail we could not read is not marked read: the user can look at it


def test_plain_text_only_mail(conn, cfg):
    r = ingest_mail(conn, cfg, FakeIMAP({"1": make_mail("just text", "<a@x>", plain_only=True)}), "run1", NOW)
    assert r.mails_failed == 1 and "no HTML part" in r.errors[0]


def test_oversize_mail_is_skipped_without_downloading(conn, cfg):
    cfg["gmail"]["max_message_bytes"] = 1000
    imap = FakeIMAP({"1": fixture_mail("alert_2026-10-30.html", "<a@x>")})
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert r.mails_failed == 1 and "over the limit" in r.errors[0] and imap.full_fetches() == []


def test_download_failure(conn, cfg):
    imap = FakeIMAP({"1": fixture_mail("alert_2026-10-30.html", "<a@x>")})
    imap.fail_download.add("1")
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert r.mails_failed == 1 and "could not download" in r.errors[0]


def test_download_failure_is_retried_next_run(conn, cfg):
    messages = {"1": fixture_mail("alert_2026-10-30.html", "<a@x>")}
    imap = FakeIMAP(messages)
    imap.fail_download.add("1")
    first = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert first.mails_failed == 1 and count(conn, "mail_messages") == 0
    second = ingest_mail(conn, cfg, FakeIMAP(messages), "run1", NOW)
    assert second.mails_ok == 1 and second.new_jobs > 0
    assert [r[0] for r in conn.execute("SELECT status FROM mail_messages")] == ["ok"]


def test_a_failed_mail_is_not_retried_every_day(conn, cfg):
    messages = {"1": make_mail("<html><body>newsletter</body></html>", "<a@x>")}
    ingest_mail(conn, cfg, FakeIMAP(messages), "run1", NOW)
    imap = FakeIMAP(messages)
    assert ingest_mail(conn, cfg, imap, "run2", NOW).mails_seen == 0 and imap.full_fetches() == []


def test_mail_without_message_id_gets_a_stable_key(conn, cfg):
    messages = {"7": make_mail((UBS / "alert_2026-10-28.html").read_text(encoding="utf-8"), message_id=None)}
    ingest_mail(conn, cfg, FakeIMAP(messages), "run1", NOW)
    again = ingest_mail(conn, cfg, FakeIMAP(messages), "run2", NOW)
    assert again.mails_seen == 0 and count(conn, "jobs") == 1
    assert conn.execute("SELECT message_id FROM mail_messages").fetchone()[0] == "uid:JobSearch/JobSearch/Alerts/UBS:7"


def test_a_mail_with_a_zero_job_table_is_ok(conn, cfg):
    html = ("These jobs matched the criteria specified for: Zurich <br><table><tr><th>Title</th><th>Job Reference #</th></tr></table>")
    r = ingest_mail(conn, cfg, FakeIMAP({"1": make_mail(html, "<a@x>")}), "run1", NOW)
    assert (r.mails_ok, r.found, r.mails_failed) == (1, 0, 0)


def test_a_bad_row_inside_a_mail_is_reported_but_the_mail_is_ok(conn, cfg):
    html = ("These jobs matched the criteria specified for: Zurich <br><table><tr><th>Title</th><th>Job Reference #</th></tr>"
            '<tr><td class="COLUMNns1">No link here</td><td class="COLUMNns1">1BR</td></tr></table>')
    r = ingest_mail(conn, cfg, FakeIMAP({"1": make_mail(html, "<a@x>")}), "run1", NOW)
    assert r.mails_ok == 1 and r.found == 0 and any("no jobid" in e for e in r.errors)


# --- folder and connection problems ----------------------------------------------------------------

def test_unknown_folder_points_to_check_mail(conn, cfg):
    cfg["gmail"]["label_ubs"] = "no-such-label"
    with pytest.raises(MailError, match="check-mail"):
        ingest_mail(conn, cfg, FakeIMAP({}), "run1", NOW)


def test_label_with_a_quote_is_quoted(conn, cfg):
    cfg["gmail"]["label_ubs"] = 'odd "label"'
    imap = FakeIMAP({}, folder='"odd \\"label\\""')
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert ("select", '"odd \\"label\\""', False) in imap.log        # quotes escaped with a backslash, whole name in quotes
    assert imap.current == '"odd \\"label\\""' and (r.mails_seen, r.mails_failed) == (0, 0)


def test_list_folders():
    assert list_folders(FakeIMAP({})) == ["INBOX", "JobSearch/JobSearch/Alerts/UBS", "JobSearch/JobSearch/Alerts/LinkedIn",
                                          "JobSearch/Alerts/Julius", "JobSearch/JobSearch/Alerts/JobsCh", "[Gmail]/Sent Mail", "Drafts"]


def test_connect_needs_the_password(cfg, monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(MailError, match="GMAIL_APP_PASSWORD"):
        mailbox.connect(cfg)


def test_connect_hides_the_password_when_login_is_refused(cfg, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "super-secret-pass")

    class Client:
        def __init__(self, host, timeout=None):
            pass

        def login(self, user, password):
            raise imaplib.IMAP4.error(f"AUTHENTICATIONFAILED {password}")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", Client)
    with pytest.raises(MailError) as exc:
        mailbox.connect(cfg)
    assert "super-secret-pass" not in str(exc.value) and "app password" in str(exc.value)
    assert exc.value.__cause__ is None


def test_connect_reports_an_unreachable_host(cfg, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-pass")

    def boom(host, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", boom)
    with pytest.raises(MailError, match="cannot reach imap.gmail.com"):
        mailbox.connect(cfg)


# --- zero-job monitoring ----------------------------------------------------------------------------

def add_mail_row(conn, n, status="ok", jobs=0, date=None):
    with conn:
        conn.execute("INSERT INTO mail_messages (message_id, source, mail_date, status, jobs_found, handled_at) VALUES (?, 'ubs_alert', ?, ?, ?, ?)",
                     (f"<{n}>", date or f"2026-10-{10 + n}T03:00:00Z", status, jobs, db.utcnow()))


def test_three_empty_mails_in_a_row_warn(conn):
    for n in range(3):
        add_mail_row(conn, n)
    assert "no jobs" in zero_job_warning(conn, "ubs_alert", 3)


def test_not_enough_mails_or_a_mix_gives_no_warning(conn):
    add_mail_row(conn, 0)
    add_mail_row(conn, 1)
    assert zero_job_warning(conn, "ubs_alert", 3) is None
    add_mail_row(conn, 2, jobs=4)
    assert zero_job_warning(conn, "ubs_alert", 3) is None


def test_only_the_latest_mails_count(conn):
    add_mail_row(conn, 0)
    add_mail_row(conn, 1)
    add_mail_row(conn, 2)
    add_mail_row(conn, 3, jobs=2)                   # newer mail with jobs ends the streak
    assert zero_job_warning(conn, "ubs_alert", 3) is None


def test_three_unreadable_mails_in_a_row_warn(conn):
    for n in range(3):
        add_mail_row(conn, n, status="error")
    assert "could not be read" in zero_job_warning(conn, "ubs_alert", 3)


# --- UBS service mails: "search agent will expire" ---------------------------------------------------

NOTICE_HTML = ("<html><body>Your search agent Zurich will expire in 10 days. Click the link below to extend the search "
               "agent for 90 days. <a href='https://example.com/renew?qs=TOKEN'>Click here to renew this agent</a></body></html>")


def notice_mail(message_id="<n1@x>", html=NOTICE_HTML, date="Mon, 19 Oct 2026 03:01:06 +0100"):
    return make_mail(html, message_id=message_id, date=date, subject="Search Agent Expiration Warning Email")


def test_expiration_warning_is_a_notice_not_an_error(conn, cfg):
    imap = FakeIMAP({"1": notice_mail(), "2": fixture_mail("alert_2026-10-28.html", "<b@x>")})
    r = ingest_mail(conn, cfg, imap, "run1", NOW)
    assert (r.mails_ok, r.mails_failed, r.found) == (1, 0, 1) and len(r.notices) == 1 and "10 days" in r.notices[0]
    row = conn.execute("SELECT status, kind, notice_days, jobs_found, error FROM mail_messages WHERE message_id = '<n1@x>'").fetchone()
    assert tuple(row) == ("ok", "notice", 10, 0, None)
    assert imap.seen() == ["2"]                    # the notice stays unread: it asks the user to do something
    assert ingest_mail(conn, cfg, FakeIMAP({"1": notice_mail()}), "run2", NOW).notices == []        # handled once


def test_notice_without_a_number_of_days(conn, cfg):
    ingest_mail(conn, cfg, FakeIMAP({"1": notice_mail(html="<html><body>Your search agent has expired.</body></html>")}), "run1", NOW)
    assert conn.execute("SELECT notice_days FROM mail_messages").fetchone()[0] is None


def test_notices_do_not_count_as_empty_alerts(conn, cfg):
    for n in range(3):
        with conn:
            conn.execute("INSERT INTO mail_messages (message_id, source, mail_date, status, jobs_found, handled_at, kind) "
                         "VALUES (?, 'ubs_alert', ?, 'ok', 0, ?, 'notice')", (f"<n{n}>", f"2026-10-1{n}T03:00:00Z", db.utcnow()))
    assert zero_job_warning(conn, "ubs_alert", 3) is None


def add_notice(conn, mail_date, days):
    with conn:
        conn.execute("INSERT INTO mail_messages (message_id, source, mail_date, status, jobs_found, handled_at, kind, notice_days) "
                     "VALUES (?, 'ubs_alert', ?, 'ok', 0, ?, 'notice', ?)", (f"<n-{mail_date}>", mail_date, db.utcnow(), days))


def test_agent_warning_before_expiry(conn):
    add_notice(conn, "2026-10-19T02:01:06Z", 10)
    text = search_agent_warning(conn, "ubs_alert", datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert "expires on 29 Oct 2026" in text and "4 days left" in text and "renew" in text


def test_agent_warning_after_expiry_without_a_later_alert(conn):
    add_notice(conn, "2026-10-19T02:01:06Z", 10)
    assert "expired on 29 Oct 2026 and no alert has arrived since" in search_agent_warning(
        conn, "ubs_alert", datetime(2026, 11, 2, tzinfo=timezone.utc))


def test_agent_warning_is_gone_once_an_alert_arrives_after_expiry(conn):
    add_notice(conn, "2026-10-19T02:01:06Z", 10)
    add_mail_row(conn, 20, jobs=3, date="2026-10-30T03:00:00Z")
    assert search_agent_warning(conn, "ubs_alert", datetime(2026, 11, 2, tzinfo=timezone.utc)) is None


def test_agent_warning_needs_a_notice_and_defaults_to_seven_days(conn):
    assert search_agent_warning(conn, "ubs_alert", NOW) is None
    add_notice(conn, "2026-10-30T00:00:00Z", None)
    assert "expires on 06 Nov 2026" in search_agent_warning(conn, "ubs_alert", NOW)


def test_the_latest_notice_decides(conn):
    add_notice(conn, "2026-10-01T00:00:00Z", 10)
    add_notice(conn, "2026-10-25T00:00:00Z", 10)
    assert "expires on 04 Nov 2026" in search_agent_warning(conn, "ubs_alert", NOW)


# --- migration 6 and the real label name ----------------------------------------------------------------

def test_migration_6_retries_old_unknown_mail_errors_only(tmp_path, monkeypatch):
    full = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", full[:5])
    c = db.connect(tmp_path / "old.db")
    for mid, status, err in (("<a>", "error", "not a recognised UBS alert: no job table header found"),
                             ("<b>", "error", "mail has no HTML part"), ("<c>", "ok", None)):
        c.execute("INSERT INTO mail_messages (message_id, source, status, jobs_found, error, handled_at) VALUES (?, 'ubs_alert', ?, 0, ?, 'x')",
                  (mid, status, err))
    c.commit()
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.migrate(c) == len(full)
    rows = {r["message_id"]: r["kind"] for r in c.execute("SELECT message_id, kind FROM mail_messages")}
    assert rows == {"<b>": "alert", "<c>": "alert"}          # the unknown-type error will be read again by the new code
    c.close()


def test_default_labels_are_the_verified_gmail_folders(cfg):
    assert cfg["gmail"]["label_ubs"] == "JobSearch/JobSearch/Alerts/UBS"
    assert cfg["gmail"]["label_linkedin"] == "JobSearch/JobSearch/Alerts/LinkedIn"


def test_a_connection_dropped_during_a_fetch_is_a_mail_error(conn, cfg):
    imap = FakeIMAP(four_alerts())
    real = imap.uid

    def uid(command, *args):
        if command == "FETCH":
            raise imaplib.IMAP4.abort("socket error: EOF")
        return real(command, *args)
    imap.uid = uid
    with pytest.raises(MailError, match="the mailbox connection failed: abort"):
        ingest_mail(conn, cfg, imap, "run1", NOW)


def test_a_timeout_on_select_is_a_mail_error(conn, cfg):
    imap = FakeIMAP({})

    def select(name, readonly=False):
        raise TimeoutError("timed out")
    imap.select = select
    with pytest.raises(MailError, match="the mailbox connection failed: TimeoutError"):
        ingest_mail(conn, cfg, imap, "run1", NOW)
