"""Reading the alert mails over IMAP (goal.md 5.1).

Only mails in the configured Gmail label and from the configured sender are read. A mail is handled once:
its Message-ID goes into `mail_messages`, and afterwards it is marked as read in the mailbox (never deleted,
never moved). The mailbox client is injected, so tests use a fake and never connect.

Each alert source is a `MailSource` (UBS, LinkedIn, Julius Baer, jobs.ch): its Gmail label and sender come from the `gmail` config section,
its parser from `app/parsers/`, and its mails are recorded under its own source key, so the zero-job rule and the
alert key (`mail_zero:<source>`) work per source. `ingest_mail` reads one source (UBS by default).

Gmail shows a label as an IMAP folder with the label's name (verified 31 Oct 2026 for the UBS and LinkedIn labels; the Julius Baer label `JobSearch/Alerts/Julius` is not under `JobSearch/JobSearch/`):
`list_folders` prints the folder names so the labels in the config can be checked.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from types import ModuleType

from . import db
from .dedup import ingest_job
from .parsers import jobsch, julius, linkedin, ubs

EXPIRY_SUBJECT = re.compile(r"search agent expir|expiration warning", re.IGNORECASE)
EXPIRY_DAYS = re.compile(r"will expire in\s+(\d+)\s+days?", re.IGNORECASE)
DEFAULT_NOTICE_DAYS = 7           # used when the warning does not say how many days are left
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass(frozen=True)
class MailSource:
    """One alert source: where its mails are (Gmail label, sender in the `gmail` config section) and how they are read."""
    name: str                   # for messages, e.g. "UBS"
    label_key: str              # gmail.<label_key>: the Gmail label (IMAP folder)
    sender_key: str             # gmail.<sender_key>: the only sender whose mails count
    parser: ModuleType          # SOURCE, parse_alert, to_job, ParseError
    adapter_label: str          # the row of this source in the run statistics
    service_notices: bool = False   # True: has service mails that are not alerts (UBS "search agent will expire")

    @property
    def key(self) -> str:
        return self.parser.SOURCE


UBS = MailSource("UBS", "label_ubs", "ubs_sender", ubs, "UBS alert mail", service_notices=True)
LINKEDIN = MailSource("LinkedIn", "label_linkedin", "linkedin_sender", linkedin, "LinkedIn alert mail")
JULIUS = MailSource("Julius Baer", "label_julius", "julius_sender", julius, "Julius Baer alert mail")
JOBSCH = MailSource("jobs.ch", "label_jobsch", "jobsch_sender", jobsch, "jobs.ch alert mail")
SOURCES = (UBS, LINKEDIN, JULIUS, JOBSCH)       # read in this order by `prepare`


class MailError(Exception):
    """The mailbox could not be read (login, folder, connection)."""


class MailAuthError(MailError):
    """Gmail refused the IMAP login, or there is no app password to log in with (the alert `gmail_auth:imap`)."""


@dataclass
class MailResult:
    source: str = ubs.SOURCE      # the source key, e.g. "ubs_alert", "linkedin_alert", "julius_alert"
    mails_seen: int = 0               # mails in the label that are new to us
    mails_ok: int = 0
    mails_failed: int = 0
    found: int = 0                    # job rows parsed from the mails
    new_jobs: int = 0                 # rows that created a new job
    outcomes: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)     # service mails (for example "search agent will expire")


def connect(cfg: dict):
    """Open an IMAP connection with the app password from the environment."""
    user = os.environ.get("GMAIL_USER") or cfg["email"]["sender"]
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        raise MailAuthError("GMAIL_APP_PASSWORD is not set")
    try:
        client = imaplib.IMAP4_SSL(cfg["gmail"]["imap_host"], timeout=30)
        client.login(user, password)
    except imaplib.IMAP4.error:
        raise MailAuthError("Gmail refused the login; check the app password and that IMAP is enabled") from None
    except (OSError, TimeoutError) as exc:
        raise MailError(f"cannot reach {cfg['gmail']['imap_host']}: {type(exc).__name__}") from None
    return client


def _imap(call, *args, **kwargs):
    """One IMAP call; a dropped connection, a timeout or a protocol error is a MailError (type name only, as in connect)."""
    try:
        return call(*args, **kwargs)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailError(f"the mailbox connection failed: {type(exc).__name__}") from None


def list_folders(client) -> list[str]:
    """Folder (label) names of the mailbox, for checking the label in the config."""
    typ, data = client.list()
    if typ != "OK":
        raise MailError("the mailbox did not list its folders")
    names = []
    for line in data or []:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        m = re.search(r'"(?:[^"\\]|\\.)*"\s*$|\S+\s*$', text)
        names.append(m.group(0).strip().strip('"') if m else text)
    return names


def _imap_date(moment: datetime) -> str:
    return f"{moment.day:02d}-{MONTHS[moment.month - 1]}-{moment.year}"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_headers(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


def _mail_date(msg) -> str:
    try:
        return parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _fetch_literal(data) -> bytes:
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return b""


def _fetch_size(data) -> int | None:
    for part in data or []:
        head = part[0] if isinstance(part, tuple) else part
        m = re.search(rb"RFC822\.SIZE (\d+)", head if isinstance(head, bytes) else str(head).encode())
        if m:
            return int(m.group(1))
    return None


def zero_job_warning(conn, source: str, n: int) -> str | None:
    """A warning when the last `n` handled mails of a source all gave no jobs, or none could be read."""
    rows = conn.execute("SELECT status, jobs_found FROM mail_messages WHERE source = ? AND kind = 'alert' "
                        "ORDER BY mail_date DESC, handled_at DESC, rowid DESC LIMIT ?", (source, n)).fetchall()
    if len(rows) < n:
        return None
    if all(r["status"] == "ok" and r["jobs_found"] == 0 for r in rows):
        return f"the last {n} alert mails of {source} contained no jobs; the saved search or the mail format may have changed"
    if all(r["status"] == "error" for r in rows):
        return f"the last {n} alert mails of {source} could not be read; the mail format may have changed"
    return None


def search_agent_warning(conn, source: str, now: datetime | None = None) -> str | None:
    """A warning while the saved search is about to expire, or has expired without any alert after it.

    UBS sends "Search Agent Expiration Warning" mails (for example "will expire in 10 days"); if the agent is not
    renewed, the alerts stop without any other sign. The expiry date is the mail date plus the stated days.
    A later alert mail shows that the agent was renewed, so then there is nothing to warn about.
    """
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT mail_date, notice_days FROM mail_messages WHERE source = ? AND kind = 'notice' "
                       "ORDER BY mail_date DESC, rowid DESC LIMIT 1", (source,)).fetchone()
    if not row or not row["mail_date"]:
        return None
    sent = datetime.strptime(row["mail_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    expires = sent + timedelta(days=row["notice_days"] or DEFAULT_NOTICE_DAYS)
    later_alert = conn.execute("SELECT 1 FROM mail_messages WHERE source = ? AND kind = 'alert' AND status = 'ok' "
                               "AND mail_date > ?", (source, expires.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
    day = expires.strftime("%d %b %Y")
    if now < expires:
        left = max(0, (expires - now).days)
        return f"UBS says the saved search expires on {day} ({left} days left): renew it in the UBS Talent Gateway, or the alerts stop"
    if later_alert:
        return None
    return f"the UBS saved search expired on {day} and no alert has arrived since: renew it in the UBS Talent Gateway"


def ingest_mail(conn, cfg: dict, client, run_id: str, now: datetime | None = None, source: MailSource = UBS) -> MailResult:
    """Read new alert mails of one source (UBS unless told otherwise), store their jobs, record each mail, mark it
    read. Returns the statistics.

    One bad mail is recorded as an error and does not stop the others. A problem with the folder or the
    connection raises MailError.
    """
    gm = cfg["gmail"]
    label, expected_sender = gm[source.label_key], gm[source.sender_key]
    now = now or datetime.now(timezone.utc)
    result = MailResult(source=source.key)
    typ, _ = _imap(client.select, _quote(label), readonly=not gm["mark_seen"])
    if typ != "OK":
        raise MailError(f"cannot open the label {label!r}; run `check-mail` to list the folder names")
    since = _imap_date(now - timedelta(days=gm["lookback_days"]))
    typ, found = _imap(client.uid, "SEARCH", None, "SINCE", since, "FROM", _quote(expected_sender))
    if typ != "OK":
        raise MailError("the mailbox search failed")
    uids = (found[0] or b"").split() if found else []

    for uid in uids:
        uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
        typ, head = _imap(client.uid, "FETCH", uid_s, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])")
        if typ != "OK":
            result.errors.append(f"uid {uid_s}: could not read the headers")
            result.mails_failed += 1
            continue
        headers = _parse_headers(_fetch_literal(head))
        message_id = (str(headers["Message-ID"] or "").strip()) or f"uid:{label}:{uid_s}"
        if conn.execute("SELECT 1 FROM mail_messages WHERE message_id = ?", (message_id,)).fetchone():
            continue
        result.mails_seen += 1
        mail_date = _mail_date(headers)
        sender = parseaddr(str(headers["From"] or ""))[1].lower()

        def record(status: str, found_n: int, error: str | None, kind: str = "alert", days: int | None = None) -> None:
            with conn:
                conn.execute("INSERT INTO mail_messages (message_id, source, mail_date, status, jobs_found, error, run_id, handled_at, kind, notice_days) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (message_id, source.key, mail_date, status, found_n, error, run_id, db.utcnow(), kind, days))

        def fail(reason: str) -> None:
            record("error", 0, reason)
            result.mails_failed += 1
            result.errors.append(f"{message_id}: {reason}")

        if sender != expected_sender.lower():
            fail(f"sender {sender!r} is not the expected {expected_sender!r}; mail ignored")
            continue
        size = _fetch_size(head)
        if size is not None and size > gm["max_message_bytes"]:
            fail(f"mail is {size} bytes, over the limit of {gm['max_message_bytes']}; skipped")
            continue
        typ, body = _imap(client.uid, "FETCH", uid_s, "(BODY.PEEK[])")
        raw = _fetch_literal(body) if typ == "OK" else b""
        if not raw:
            # transient: no row is recorded (as for a header failure), so the next run reads the mail again
            result.errors.append(f"{message_id}: could not download the mail (will retry)")
            result.mails_failed += 1
            result.mails_seen -= 1
            continue
        try:
            part = email.message_from_bytes(raw, policy=policy.default).get_body(("html",))
            html = part.get_content() if part is not None else None
        except (LookupError, ValueError, UnicodeDecodeError) as exc:
            fail(f"mail body could not be decoded: {type(exc).__name__}")
            continue
        if not html:
            fail("mail has no HTML part")
            continue
        if source.service_notices and EXPIRY_SUBJECT.search(str(headers["Subject"] or "")):
            # UBS service mail, not a job alert. Recorded as a notice and left unread so that the user sees it.
            m = EXPIRY_DAYS.search(re.sub(r"<[^>]+>", " ", html))
            days = int(m.group(1)) if m else None
            record("ok", 0, None, kind="notice", days=days)
            result.notices.append(f"{message_id}: UBS search agent expiration warning" + (f" ({days} days)" if days else ""))
            continue
        try:
            parsed = source.parser.parse_alert(html)
        except source.parser.ParseError as exc:
            fail(f"not a recognised {source.name} alert: {exc}")
            continue

        with conn:      # ingest is idempotent, so a crash before the mail record is written just re-reads this mail
            for job in parsed.jobs:
                outcome = ingest_job(conn, source.parser.to_job(job, message_id=message_id), run_id)
                result.outcomes[outcome.outcome] = result.outcomes.get(outcome.outcome, 0) + 1
                result.new_jobs += outcome.outcome == "new"
            conn.execute("INSERT INTO mail_messages (message_id, source, mail_date, status, jobs_found, error, run_id, handled_at, kind) "
                         "VALUES (?, ?, ?, 'ok', ?, ?, ?, ?, 'alert')",
                         (message_id, source.key, mail_date, len(parsed.jobs),
                          "; ".join(parsed.errors) or None, run_id, db.utcnow()))
        result.mails_ok += 1
        result.found += len(parsed.jobs)
        result.errors.extend(f"{message_id}: {e}" for e in parsed.errors)
        if gm["mark_seen"]:
            _imap(client.uid, "STORE", uid_s, "+FLAGS", "(\\Seen)")
    return result
