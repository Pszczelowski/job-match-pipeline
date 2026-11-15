"""Health alerts by e-mail (goal.md 5.9).

A condition has a stable key (`run_failed:prepare`, `adapter_zero:six`, `mail_zero:ubs_alert`, `mail_zero:linkedin_alert`, `mail_zero:julius_alert`, `mail_zero:jobsch_alert`,
`gmail_auth:imap`, `gmail_auth:smtp`, `backup_failed`, `run_interrupted:prepare`, `run_interrupted:finish`, `missed_schedule`). The commands *record* what they observe (`observe`, `clear`, `set_state`) in the
table `alerts`; `deliver` then sends the mails. Recording and sending are separate: with `monitoring.alerts` false
the alerts are still recorded (and listed by `status`), but nothing is sent.

Rules of `deliver` (the exact rules are in goal.md 5.9):
* A "raised" mail is due for an open alert when none was sent yet, or the last one was sent on an earlier calendar day
  (in `timezone` of the config). So: at most one raised mail per key per day, and one more each following day while
  the condition lasts.
* A condition that clears closes the alert. If the user was told about it (a raised mail of this incident went out),
  one "resolved" mail follows. An alert that was never mailed is closed silently.
* A mail that cannot be sent leaves the alert as it was, so the next command tries again. The first failure ends the
  attempts of that call (the mail server is not asked again for every alert). Nothing here raises: an alert must never
  make a command fail.

The mails go through `report.smtp_send` (the report's SMTP code, retries and password scrubbing). They carry no job text,
no CV text, no home address and never the app password: every stored message passes `safe_text`.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import adapters
from .config import resolve_path
from .headless import RUN_ID_RE
from .mailbox import SOURCES as MAIL_SOURCES, zero_job_warning
from .report import MONTHS, ReportAuthError, new_message, smtp_send

log = logging.getLogger("recruitment")

STAMP = "%Y-%m-%dT%H:%M:%SZ"
MAX_MESSAGE = 300
SMTP_AUTH_KEY = "gmail_auth:smtp"
IMAP_AUTH_KEY = "gmail_auth:imap"
BACKUP_KEY = "backup_failed"
MAIL_SOURCE = "ubs_alert"
HEADLESS_AUTH_KEY = "headless_auth"
HEADLESS_LIMIT_KEY = "headless_usage_limit"
HEADLESS_FAILED_KEY = "headless_failed"
MISSED_SCHEDULE_KEY = "missed_schedule"

HINTS = {
    "run_failed": "Look in the log file (paths.logs) for the run id and the step that failed, fix it and run the command again.",
    "adapter_zero": "Open the employer's job list in a browser. If it still shows jobs, the page layout probably changed: run "
                    "`python -m app check-adapter <name>` to see what the program reads.",
    "mail_zero": "Check the saved search at UBS and the mails in the Gmail label; the mail format may have changed.",
    "mail_zero:linkedin_alert": "Check the job alert on LinkedIn and the mails in the Gmail label; LinkedIn may have changed the mail format.",
    "mail_zero:julius_alert": "Check the job alert on the Julius Baer career site and the mails in the Gmail label; the mail format may have changed.",
    "mail_zero:jobsch_alert": "Check the job alerts at jobs.ch and the mails in the Gmail label; the mail format may have changed.",
    "gmail_auth": "Create a new Gmail app password, put it in .env as GMAIL_APP_PASSWORD, and check that IMAP is enabled.",
    "headless_auth": "The Claude token is missing, expired or refused: make a new one with `claude setup-token` on the PC and replace the line "
                     "CLAUDE_CODE_OAUTH_TOKEN in llm.env on the NAS (the llm service reads it again for the next request).",
    "headless_usage_limit": "The plan's usage limit stopped the headless run; the unscored jobs carry over into the next batch. "
                            "Nothing to do unless it happens every day.",
    "headless_failed": "Read the done file in exchange/llm/done and the log of the llm service; unfinished CVs or letters stay in "
                       "exchange/llm/work/<run> and can be finished in a Claude session on the PC (option C).",
    "run_interrupted": "The container was stopped, restarted or redeployed during the run, or the step ran over 5 hours. Do not redeploy "
                       "during the daily run (DEPLOYMENT_MANUAL 7); the next prepare collects what waits.",
    "missed_schedule": "Check that the recruitment container runs (UGOS Docker or `docker compose ps`) and that /health shows a fresh "
                       "scheduler heartbeat; catch up with `docker compose exec recruitment python -m app prepare`.",
    "backup": "Run `python -m app backup` by hand and read the error; check the free space of the disk that holds backup.dir.",
}


@dataclass
class DeliverResult:
    sent: list[str] = field(default_factory=list)          # keys of the raised mails that went out
    resolved: list[str] = field(default_factory=list)      # keys of the resolved mails that went out
    failed: str | None = None                              # why sending stopped, if it did
    disabled: bool = False                                 # monitoring.alerts is false: nothing was tried
    auth: bool = False                                     # the failure was a refused SMTP login (or no app password)


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(STAMP)


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)


def _local_day(stamp: str, tz: str) -> date:
    return _parse(stamp).astimezone(ZoneInfo(tz)).date()


def _local_text(stamp: str, tz: str) -> str:
    d = _parse(stamp).astimezone(ZoneInfo(tz))
    return f"{d.day} {MONTHS[d.month - 1]} {d.year} {d:%H:%M} ({tz})"


def safe_text(cfg: dict, text: str) -> str:
    """One line, at most 300 characters, without the app password or the home street address."""
    secrets = [os.environ.get("GMAIL_APP_PASSWORD") or "", cfg.get("location", {}).get("car_origin_address") or ""]
    for secret in secrets:
        for variant in {secret.strip(), secret.replace(" ", "").strip()}:
            if variant:
                text = text.replace(variant, "***")
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 3] + "..."


# --- recording -------------------------------------------------------------------------------------

def observe(conn, cfg: dict, key: str, message: str, now: datetime | None = None) -> None:
    """The condition `key` holds in this run. Opens the alert, or counts one more sighting. A resolved alert of the
    same key is reopened as a new incident (first_seen and count start again; `last_sent` is kept, so the
    one-mail-per-day rule still holds)."""
    stamp, message = _stamp(now), safe_text(cfg, message)
    try:
        with conn:
            row = conn.execute("SELECT resolved_at FROM alerts WHERE key = ?", (key,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO alerts (key, first_seen, last_seen, message, count) VALUES (?, ?, ?, ?, 1)",
                             (key, stamp, stamp, message))
            elif row["resolved_at"] is not None:
                conn.execute("UPDATE alerts SET first_seen = ?, last_seen = ?, resolved_at = NULL, resolved_sent_at = NULL, "
                             "message = ?, count = 1 WHERE key = ?", (stamp, stamp, message, key))
            else:
                conn.execute("UPDATE alerts SET last_seen = ?, message = ?, count = count + 1 WHERE key = ?", (stamp, message, key))
    except sqlite3.Error as exc:
        log.error("alert %s could not be recorded: %s", key, type(exc).__name__)


def clear(conn, key: str, now: datetime | None = None) -> None:
    """The condition `key` is gone. Closes the alert if it was open; nothing happens if it was not."""
    try:
        with conn:
            conn.execute("UPDATE alerts SET resolved_at = ? WHERE key = ? AND resolved_at IS NULL", (_stamp(now), key))
    except sqlite3.Error as exc:
        log.error("alert %s could not be closed: %s", key, type(exc).__name__)


def observe_headless(conn, cfg: dict, done: dict | None, now: datetime | None = None) -> None:
    """The three alerts of the headless chain (v0.78.0), from the done file of the llm service. A run without a done file
    (headless off) changes nothing; a good run closes all three."""
    if not done:
        return
    statuses = [(done.get("run_id"), done.get("status"), done.get("message") or "")] + [
        (r.get("run_id"), r.get("status"), r.get("message") or "") for r in done.get("resumed", []) if isinstance(r, dict)]
    bad = [(rid, s, m) for rid, s, m in statuses if s not in ("ok", None)]
    if not bad:
        for key in (HEADLESS_AUTH_KEY, HEADLESS_LIMIT_KEY, HEADLESS_FAILED_KEY):
            clear(conn, key, now)
        return
    wanted = {"auth": HEADLESS_AUTH_KEY, "usage_limit": HEADLESS_LIMIT_KEY}
    groups: dict[str, list] = {}
    for item in bad:                                              # one sighting per key, so the text is the first run's, not the last one's
        groups.setdefault(wanted.get(item[1], HEADLESS_FAILED_KEY), []).append(item)
    parts = [done] + [r for r in done.get("resumed", []) if isinstance(r, dict)]      # the reset time may sit in a resumed part
    limit_text = next((p.get("limit_message") for p in parts if p.get("status") == "usage_limit" and p.get("limit_message")),
                      None) or "usage limit reached"
    for key, group in groups.items():
        run_id, status, message = group[0]
        text = {"auth": "the headless Claude login was refused", "usage_limit": limit_text}.get(
            status, f"headless run ended with status {status}" + (f": {message}" if message else ""))
        known = isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)
        prefix = f"run {run_id} (exchange/llm/done/{run_id}.json): " if known else ""      # first: the 300-character cut keeps it
        others = [str(g[0]) for g in group[1:] if g[0]]
        extra = f" (and {len(group) - 1} more run(s): {', '.join(others)})" if len(group) > 1 and others else ""
        observe(conn, cfg, key, prefix + text + extra, now)
    # a run of another kind shows that the earlier condition is gone: a limit message means the login worked, a good or
    # partial run means neither the login nor the limit stopped it (`headless_failed` closes only on a fully good run)
    states = {s for _rid, s, _m in statuses}
    if "auth" not in states and states & {"ok", None, "usage_limit", "partial"}:
        clear(conn, HEADLESS_AUTH_KEY, now)
    if "usage_limit" not in states and states & {"ok", None, "partial"}:
        clear(conn, HEADLESS_LIMIT_KEY, now)


def set_state(conn, cfg: dict, key: str, problem: str | None, now: datetime | None = None) -> None:
    """`problem` is the text while the condition holds, None when it does not."""
    if problem:
        observe(conn, cfg, key, problem, now)
    else:
        clear(conn, key, now)


def adapter_problem(conn, label: str, n: int, empty_ok: bool = False) -> str | None:
    """Text when the last `n` reads of an adapter all returned no jobs or failed (needs `n` recorded reads).
    With `empty_ok` (a site that may really have no vacancy) only failed reads count."""
    rows = conn.execute("SELECT found, error FROM adapter_runs WHERE adapter = ? ORDER BY id DESC LIMIT ?", (label, n)).fetchall()
    if len(rows) < n or not all(r["error"] if empty_ok else (r["found"] == 0 or r["error"]) for r in rows):
        return None
    failed = sum(1 for r in rows if r["error"])
    if empty_ok:
        return f"the last {n} reads of {label} failed; the site or its page layout may have changed"
    return (f"the last {n} reads of {label} returned no jobs ({failed} of them failed); "
            "the site or its page layout may have changed")


def check_sources(conn, cfg: dict, *, mail_read: bool | set[str], now: datetime | None = None) -> None:
    """Zero-result conditions, evaluated at the end of `prepare`: the alert mails of each source (only a source this
    run could read; `mail_read` is True for all sources, False for none, or the set of source keys that were read)
    and every employer adapter (an adapter that is switched off has no alert)."""
    n = cfg["monitoring"]["zero_jobs_alert_after"]
    for source in MAIL_SOURCES:
        if mail_read is True or (mail_read is not False and source.key in mail_read):
            set_state(conn, cfg, f"mail_zero:{source.key}", zero_job_warning(conn, source.key, n), now)
    on = {spec.key for spec in adapters.enabled_adapters(cfg)}
    for spec in adapters.REGISTRY:
        set_state(conn, cfg, f"adapter_zero:{spec.key}", adapter_problem(conn, spec.label, n, spec.empty_is_normal) if spec.key in on else None, now)


def open_alerts(conn) -> list[dict]:
    """The unresolved alerts, oldest first (for `status`)."""
    return [{"key": r["key"], "first_seen": r["first_seen"], "last_seen": r["last_seen"], "count": r["count"],
             "mailed": r["last_sent"] is not None, "message": r["message"]}
            for r in conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY first_seen, id")]


# --- mails -----------------------------------------------------------------------------------------

def short_title(key: str) -> str:
    kind, _, arg = key.partition(":")
    if kind == "run_failed":
        return f"{arg} failed"
    if kind == "adapter_zero":
        spec = next((s for s in adapters.REGISTRY if s.key == arg), None)
        return f"{spec.label if spec else arg} returned no jobs"
    if kind == "mail_zero":
        source = next((s for s in MAIL_SOURCES if s.key == arg), None)
        return f"{source.name} alert mails contained no jobs" if source else f"{arg} mails contained no jobs"
    if kind == "gmail_auth":
        return f"Gmail {arg.upper()} login refused"
    if kind == "run_interrupted":
        return f"{arg} was interrupted"
    if key == BACKUP_KEY:
        return "backup failed"
    if key == MISSED_SCHEDULE_KEY:
        return "no successful prepare for more than 30 hours"
    return {HEADLESS_AUTH_KEY: "headless Claude login refused", HEADLESS_LIMIT_KEY: "headless Claude stopped at the usage limit",
            HEADLESS_FAILED_KEY: "headless Claude run failed"}.get(key, key)


def _hint(key: str) -> str:
    kind = key.partition(":")[0]
    return HINTS.get(key) or HINTS.get("backup" if key == BACKUP_KEY else kind, "")


def _subject(cfg: dict, key: str, resolved: bool) -> str:
    return f"{cfg['report']['subject_prefix']} alert{' resolved' if resolved else ''}: {short_title(key)}"


def raised_message(cfg: dict, row):
    tz = cfg["timezone"]
    last_seen = row["last_seen"] if "last_seen" in row.keys() else None
    lines = [short_title(row["key"]), "", row["message"], "", f"First seen: {_local_text(row['first_seen'], tz)}",
             *([f"Last seen: {_local_text(last_seen, tz)}"] if last_seen else []),
             f"Seen {row['count']} time(s) since then."]
    if _hint(row["key"]):
        lines += ["", "What to do: " + _hint(row["key"])]
    lines += ["", "You get at most one mail a day about this problem while it lasts, and one more when it is resolved.",
              "`python -m app status` lists the open alerts."]
    msg = new_message(cfg, _subject(cfg, row["key"], False))
    msg["X-Recruitment-Alert"] = row["key"]
    msg.set_content("\n".join(lines) + "\n")
    return msg


def resolved_message(cfg: dict, row):
    tz = cfg["timezone"]
    lines = [f"{short_title(row['key'])}: resolved", "",
             f"The problem is gone. It was first seen {_local_text(row['first_seen'], tz)} and seen {row['count']} time(s); "
             f"it cleared {_local_text(row['resolved_at'], tz)}.", "", f"Last message: {row['message']}"]
    msg = new_message(cfg, _subject(cfg, row["key"], True))
    msg["X-Recruitment-Alert"] = row["key"]
    msg.set_content("\n".join(lines) + "\n")
    return msg


# --- sending ---------------------------------------------------------------------------------------

def _origin_run(message: str) -> str:
    """The run an alert was raised for: the `run <id> (` prefix of its message (headless alerts), else `-`."""
    m = re.match(r"run (\S+) \(", message or "")
    return m.group(1) if m else "-"


def deliver(conn, cfg: dict, *, smtp_factory=smtplib.SMTP, sleep=time.sleep, now: datetime | None = None,
            keys: set[str] | None = None) -> DeliverResult:
    """Send the mails that are due. Never raises. With `keys` only the alerts of those keys are mailed (the web supervisor's
    hourly check sends `missed_schedule` and nothing else, so other reminders keep coming with the daily run)."""
    result = DeliverResult()
    if not cfg["monitoring"]["alerts"]:
        result.disabled = True
        return result
    try:
        _deliver(conn, cfg, smtp_factory, sleep, now, result, keys)
    except Exception as exc:                                     # an alert must never make a command fail
        result.failed = result.failed or type(exc).__name__
        log.error("alert mails: stopped by %s", type(exc).__name__)
    return result


def _send(cfg: dict, msg, smtp_factory, sleep, key: str, result: DeliverResult) -> bool:
    """True if sent. On failure: log, remember why in `result`, and report an SMTP login refusal as its own alert."""
    try:
        smtp_send(cfg, msg, smtp_factory, sleep)
        return True
    except ReportAuthError as exc:
        result.failed = exc.detail
        result.auth = True
        log.error("alert mail for %s not sent: %s", key, exc.detail)
    except Exception as exc:                                     # ReportError, or anything a mail server library throws
        result.failed = getattr(exc, "detail", None) or type(exc).__name__
        log.error("alert mail for %s not sent: %s", key, safe_text(cfg, result.failed))
    return False


def _deliver(conn, cfg: dict, smtp_factory, sleep, now: datetime | None, result: DeliverResult, keys: set[str] | None = None) -> None:
    stamp, tz = _stamp(now), cfg["timezone"]
    today = _local_day(stamp, tz)
    mailed = False
    due = [r for r in conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY first_seen, id")
           if (keys is None or r["key"] in keys) and (r["last_sent"] is None or _local_day(r["last_sent"], tz) < today)]
    for row in due:
        if not _send(cfg, raised_message(cfg, row), smtp_factory, sleep, row["key"], result):
            break
        with conn:
            conn.execute("UPDATE alerts SET last_sent = ? WHERE id = ?", (stamp, row["id"]))
        result.sent.append(row["key"])
        mailed = True
        log.info("event=alert_mail kind=%s origin_run=%s key=%s title=%s", "new" if row["last_sent"] is None else "reminder",
                 _origin_run(row["message"]), row["key"], short_title(row["key"]))
    if mailed:
        clear(conn, SMTP_AUTH_KEY, now)                          # a mail went out, so the SMTP login works again
    elif result.auth:
        observe(conn, cfg, SMTP_AUTH_KEY, "Gmail refused the SMTP login while an alert mail was sent: "
                + (result.failed or ""), now)
    if result.failed:
        return
    # the user was told about this incident (last_sent is not older than first_seen) and has not heard that it ended
    for row in conn.execute("SELECT * FROM alerts WHERE resolved_at IS NOT NULL AND resolved_sent_at IS NULL "
                            "AND last_sent IS NOT NULL AND last_sent >= first_seen ORDER BY resolved_at, id").fetchall():
        if keys is not None and row["key"] not in keys:
            continue
        if not _send(cfg, resolved_message(cfg, row), smtp_factory, sleep, row["key"], result):
            break
        with conn:
            conn.execute("UPDATE alerts SET resolved_sent_at = ? WHERE id = ?", (stamp, row["id"]))
        result.resolved.append(row["key"])
        log.info("event=alert_resolved_mail origin_run=%s key=%s title=%s", _origin_run(row["message"]), row["key"], short_title(row["key"]))


# --- the backup command: no migration allowed -----------------------------------------------------

def open_without_migration(cfg: dict):
    """The live database as it is (`backup` must never migrate it), or None if it is missing, damaged or has no
    `alerts` table yet."""
    path = resolve_path(cfg["paths"]["database"])
    if not Path(path).is_file():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alerts'").fetchone() is None:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def report_backup(cfg: dict, error: str | None, *, smtp_factory=smtplib.SMTP, sleep=time.sleep,
                  now: datetime | None = None) -> None:
    """Record the outcome of `python -m app backup` and send what is due. A database that cannot be opened without a
    migration cannot hold the alert: a failure is then mailed directly, once, without a later "resolved" mail."""
    try:
        conn = open_without_migration(cfg)
        if conn is None:
            if error and cfg["monitoring"]["alerts"]:
                row = {"key": BACKUP_KEY, "message": safe_text(cfg, f"the backup failed: {error}"), "first_seen": _stamp(now), "count": 1}
                result = DeliverResult()
                if _send(cfg, raised_message(cfg, row), smtp_factory, sleep, BACKUP_KEY, result):
                    log.info("alert mail sent (not recorded): %s", short_title(BACKUP_KEY))
            elif error:
                log.warning("backup failed and the alert could not be recorded (database not usable without a migration)")
            return
        try:
            set_state(conn, cfg, BACKUP_KEY, f"the backup failed: {error}" if error else None, now)
            deliver(conn, cfg, smtp_factory=smtp_factory, sleep=sleep, now=now)
        finally:
            conn.close()
    except Exception as exc:                                     # never change the exit code of the command
        log.error("alert for the backup could not be handled: %s", type(exc).__name__)
